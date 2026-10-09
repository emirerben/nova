"""Live plan block feed (KRI-443, contract v2 KRI-439): `plan_block` events on a v2 thread.

After Create, the render pipeline reports what it decided, section by section.
iOS reads these through the existing `GET /creation-threads/{id}/delta` poll, and a
cold client reads the reduced state from `GET /creation-threads/{id}/plan`
(`plan_snapshot.py`). Contract: docs/pipelines/live-plan-blocks.md; the wire models are
in `plan_contract.py` (frozen, shared with the Swift mirror).

Invariants:
* Dark behind `LIVE_PLAN_REVIEW_ENABLED`; off = no event, no query, no side effect.
* `emit_plan_blocks` is best-effort and NEVER raises: a feed problem must not
  fail a render.
* It opens its OWN short session and locks only the CreationThread row (last in
  `app/db_locks.CANONICAL_LOCK_ORDER`). Never call it while the caller holds a
  Job/PlanItem/Plan row lock (the `record_pipeline_event` lock trap), and call it
  from the main render thread, after the surrounding `db.commit()`.
* `revision` / `changed` / `previous` are computed in ONE place, `_emit`, under the
  thread lock (`annotate_blocks`). Emit sites never compute them.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from app.config import settings
from app.database import sync_session
from app.kria import plan_payloads
from app.kria.plan_contract import DETAIL_MAX, SCOPABLE_SECTIONS, SUMMARY_MAX
from app.kria.plan_contract import SECTION_ORDER as _CONTRACT_SECTION_ORDER

log = structlog.get_logger()

SECTION_ORDER: tuple[str, ...] = _CONTRACT_SECTION_ORDER
STATES = ("waiting", "deciding", "decided")
STATE_RANK = {"waiting": 0, "deciding": 1, "decided": 2}
EVENT_TYPE = "plan_block"
SUMMARY_EVENT_TYPE = "plan_update_summary"
_SUMMARY_MAX = SUMMARY_MAX
_DETAIL_MAX = DETAIL_MAX
POST_CAPTION_TIMEOUT_S = 8.0
_EVENT_SCAN_LIMIT = 600


def block(
    section_id: str,
    state: str,
    summary: str | None = None,
    detail: str | None = None,
    *,
    intent: bool = False,
    skipped: bool = False,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One contract block. `decided_at` is stamped only for `decided`.

    `payload` is kept only for a decided, non-skipped block; flag-off producers never pass
    one, so their blocks keep exactly the original seven keys.
    """
    if section_id not in SECTION_ORDER:
        raise ValueError(f"unknown plan block section: {section_id}")
    if state not in STATES:
        raise ValueError(f"unknown plan block state: {state}")
    out: dict[str, Any] = {
        "section_id": section_id,
        "state": state,
        "summary": (summary or None) and str(summary)[:_SUMMARY_MAX],
        "detail": (detail or None) and str(detail)[:_DETAIL_MAX],
        "intent": bool(intent),
        "skipped": bool(skipped),
        "decided_at": datetime.now(UTC).isoformat() if state == "decided" else None,
    }
    if payload is not None and state == "decided" and not skipped:
        out["payload"] = payload
    return out


def waiting_blocks() -> list[dict[str, Any]]:
    return [block(section, "waiting") for section in SECTION_ORDER]


def plan_block_payload(
    *,
    turn_id: str | None,
    job_id: str,
    blocks: list[dict[str, Any]],
    scope: list[str] | None = None,
    previous_job_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"turn_id": turn_id, "job_id": str(job_id), "blocks": blocks}
    if scope:
        payload["scope"] = list(scope)
    if previous_job_id:
        payload["previous_job_id"] = str(previous_job_id)
    return payload


def enabled() -> bool:
    return bool(settings.live_plan_review_enabled)


# ── revision / changed / previous (one place) ────────────────────────────────


def block_revision(value: dict[str, Any] | None) -> int:
    """A decided block written before contract v2 has no revision; it counts as 1."""
    if not value:
        return 0
    revision = value.get("revision")
    if isinstance(revision, int) and revision > 0:
        return revision
    return 1 if value.get("state") == "decided" else 0


def same_value(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Equal decided values: canonical payload JSON, else summary + skipped."""
    pa, pb = a.get("payload"), b.get("payload")
    if bool(a.get("skipped")) != bool(b.get("skipped")):
        return False
    if pa is not None and pb is not None:
        return plan_payloads.payload_equal(pa, pb)
    return (a.get("summary") or None) == (b.get("summary") or None)


def iter_blocks(events: list[dict[str, Any]]):
    """(event payload, block) pairs from `plan_block` event payloads, in order."""
    for payload in events:
        if not isinstance(payload, dict):
            continue
        for entry in payload.get("blocks") or []:
            if isinstance(entry, dict) and entry.get("section_id") in SECTION_ORDER:
                yield payload, entry


def latest_decided_other_job(
    events: list[dict[str, Any]], job_id: str
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Per section, the newest decided block written by a job other than `job_id`."""
    found: dict[str, tuple[str, dict[str, Any]]] = {}
    for payload, entry in iter_blocks(events):
        if str(payload.get("job_id")) == job_id or entry.get("state") != "decided":
            continue
        found[str(entry["section_id"])] = (str(payload.get("job_id")), entry)
    return found


def previous_job_id_of(events: list[dict[str, Any]], job_id: str) -> str | None:
    previous = None
    for payload in events:
        if isinstance(payload, dict) and payload.get("job_id") and str(payload["job_id"]) != job_id:
            previous = str(payload["job_id"])
    return previous


def scope_of(events: list[dict[str, Any]], job_id: str) -> list[str] | None:
    for payload in events:
        if (
            isinstance(payload, dict)
            and str(payload.get("job_id")) == job_id
            and isinstance(payload.get("scope"), list)
            and payload["scope"]
        ):
            return [str(s) for s in payload["scope"]]
    return None


def annotate_blocks(
    blocks: list[dict[str, Any]], events: list[dict[str, Any]], job_id: str
) -> list[dict[str, Any]]:
    """Stamp `revision`, `changed` and `previous` (contract section 1.5). Pure.

    * First decided value in a thread: revision 1, unchanged, no previous.
    * Equal to the previous job's value: same revision, unchanged.
    * Different: revision + 1, `changed`, `previous` = what it replaces.
    * waiting / deciding carry the revision of the value they will replace (0 if none).
    * A re-emit within the same job never lowers the revision already written.
    """
    previous = latest_decided_other_job(events, job_id)
    same_job: dict[str, dict[str, Any]] = {}
    for payload, entry in iter_blocks(events):
        if str(payload.get("job_id")) == job_id and entry.get("state") == "decided":
            same_job[str(entry["section_id"])] = entry
    out: list[dict[str, Any]] = []
    for original in blocks:
        entry = dict(original)
        section = str(entry["section_id"])
        prior = previous.get(section)
        prior_revision = block_revision(prior[1]) if prior else 0
        entry["changed"] = False
        entry.pop("previous", None)
        if entry.get("state") != "decided":
            entry["revision"] = prior_revision
        elif prior is None:
            entry["revision"] = 1
        elif same_value(entry, prior[1]):
            entry["revision"] = prior_revision
        else:
            entry["revision"] = prior_revision + 1
            entry["changed"] = True
            entry["previous"] = {
                "revision": prior_revision,
                "job_id": prior[0],
                "summary": prior[1].get("summary"),
                "payload": prior[1].get("payload"),
                "skipped": bool(prior[1].get("skipped")),
            }
        written = same_job.get(section)
        if written is not None and entry["revision"] < block_revision(written):
            entry["revision"] = block_revision(written)
            entry["changed"] = bool(written.get("changed"))
            if written.get("previous") is not None:
                entry["previous"] = written["previous"]
        out.append(entry)
    return out


def freeze_out_of_scope(
    blocks: list[dict[str, Any]],
    events: list[dict[str, Any]],
    job_id: str,
    scope: list[str] | None,
) -> list[dict[str, Any]]:
    """A scoped update never touches the sections outside its scope.

    The dispatch event already copied them as `decided` (`changed=false`). A render path
    that re-reports one of them (it runs the whole pipeline) must not turn that copy into a
    fake "Updated" value, so such a block is dropped. A section that was NOT decided at
    dispatch (it had no previous value) is still free to be filled in.
    """
    if not scope:
        return blocks
    copied = {
        str(entry["section_id"])
        for payload, entry in iter_blocks(events)
        if str(payload.get("job_id")) == job_id and entry.get("state") == "decided"
    }
    kept = [
        entry
        for entry in blocks
        if entry.get("section_id") in scope or entry.get("section_id") not in copied
    ]
    if len(kept) != len(blocks):
        log.info(
            "plan_block_out_of_scope_ignored",
            job_id=job_id,
            sections=[e["section_id"] for e in blocks if e not in kept],
        )
    return kept


def merge_block(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """Server reducer, the same forward-only rule as the client.

    Higher revision replaces the whole block; equal revision moves forward only
    (waiting -> deciding -> decided) and fills in, never blanks, summary/detail/payload;
    a lower revision is ignored.
    """
    if existing is None:
        return dict(incoming)
    ri, re = block_revision(incoming), block_revision(existing)
    if ri > re:
        return dict(incoming)
    if ri < re:
        return existing
    if STATE_RANK.get(incoming.get("state"), 0) < STATE_RANK.get(existing.get("state"), 0):
        return existing
    merged = dict(incoming)
    for key in ("summary", "detail", "payload"):
        if merged.get(key) is None and existing.get(key) is not None:
            merged[key] = existing[key]
    return merged


def reduce_job_blocks(events: list[dict[str, Any]], job_id: str) -> dict[str, dict[str, Any]]:
    reduced: dict[str, dict[str, Any]] = {}
    for payload, entry in iter_blocks(events):
        if str(payload.get("job_id")) != job_id:
            continue
        section = str(entry["section_id"])
        reduced[section] = merge_block(reduced.get(section), entry)
    return reduced


# ── emit ─────────────────────────────────────────────────────────────────────


def load_plan_events(db: Any, thread_id: uuid.UUID) -> list[dict[str, Any]]:
    """The thread's `plan_block` event payloads, oldest first, newest 600 at most."""
    from app.models import CreationThreadEvent  # noqa: PLC0415

    rows = list(
        db.execute(
            select(CreationThreadEvent.payload)
            .where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.event_type == EVENT_TYPE,
            )
            .order_by(CreationThreadEvent.sequence.desc())
            .limit(_EVENT_SCAN_LIMIT)
        ).scalars()
    )
    rows.reverse()
    return [row for row in rows if isinstance(row, dict)]


def _thread_id_for(db: Any, job: Any) -> uuid.UUID | None:
    from app.models import CreationThread  # noqa: PLC0415

    item_id = getattr(job, "content_plan_item_id", None)
    if item_id is None:
        return None
    return db.execute(
        select(CreationThread.id)
        .where(
            CreationThread.active_plan_item_id == item_id,
            CreationThread.creator_id == job.user_id,
            CreationThread.runtime_version == 2,
        )
        .limit(1)
    ).scalar_one_or_none()


def emit_plan_blocks(job_id: str | uuid.UUID, blocks: list[dict[str, Any]]) -> None:
    """Append one `plan_block` event for `job_id`'s thread. Best-effort, never raises."""
    if not enabled() or not blocks:
        return
    try:
        written = _emit(job_id, blocks)
    except Exception as exc:  # noqa: BLE001 - the feed must never fail a render
        log.warning(
            "plan_blocks_emit_failed",
            job_id=str(job_id),
            error_class=type(exc).__name__,
            error=str(exc)[:200],
        )
        return
    if not written:
        return
    try:
        log.info(
            "plan_block_emitted",
            job_id=str(job_id),
            sections=[b["section_id"] for b in written["blocks"]],
            states=[b["state"] for b in written["blocks"]],
            changed_sections=[b["section_id"] for b in written["blocks"] if b.get("changed")],
        )
        _maybe_start_post_caption(job_id, written)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_blocks_after_emit_failed", job_id=str(job_id), error=str(exc)[:200])


def _emit(job_id: str | uuid.UUID, blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Append the event; returns {blocks, mode, thread_id} for post-commit hooks."""
    from app.models import (  # noqa: PLC0415
        CreationThread,
        CreatorAgentExecution,
        CreatorAgentTurn,
        Job,
    )
    from app.tasks.kria_runtime import _append_sync_event  # noqa: PLC0415

    job_uuid = uuid.UUID(str(job_id))
    with sync_session() as db:
        job = db.get(Job, job_uuid)
        if job is None or job.status == "cancelled":
            return None
        thread_id = _thread_id_for(db, job)
        if thread_id is None:
            return None
        turn_id = db.execute(
            select(CreatorAgentExecution.turn_id)
            .where(
                CreatorAgentExecution.target_job_id == job_uuid,
                CreatorAgentExecution.turn_id.is_not(None),
            )
            .order_by(CreatorAgentExecution.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if turn_id is None:
            turn_id = db.execute(
                select(CreatorAgentTurn.id)
                .where(
                    CreatorAgentTurn.thread_id == thread_id,
                    CreatorAgentTurn.status.in_(("executing", "observing")),
                )
                .order_by(CreatorAgentTurn.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        # Thread is the LAST lock in the canonical order and the only one taken here.
        thread = db.execute(
            select(CreationThread).where(CreationThread.id == thread_id).with_for_update()
        ).scalar_one_or_none()
        if thread is None or int(thread.runtime_version) != 2:
            return None
        job_key = str(job_uuid)
        events = load_plan_events(db, thread_id)
        scope = scope_of(events, job_key)
        blocks = freeze_out_of_scope(blocks, events, job_key, scope)
        if not blocks:
            return None
        annotated = annotate_blocks(blocks, events, job_key)
        _append_sync_event(
            db,
            thread,
            role="system",
            event_type=EVENT_TYPE,
            content=None,
            payload=plan_block_payload(
                turn_id=str(turn_id) if turn_id else None,
                job_id=job_key,
                blocks=annotated,
                scope=scope,
                previous_job_id=previous_job_id_of(events, job_key),
            ),
        )
        mode = getattr(job, "mode", None)
        db.commit()
    return {
        "blocks": annotated,
        "mode": mode,
        "thread_id": str(thread_id),
        "scoped": bool(scope),
    }


def dispatch_payload(
    db: Any,
    thread: Any,
    *,
    turn_id: str | None,
    job_id: str,
    source_event_id: uuid.UUID | None,
) -> dict[str, Any]:
    """The first `plan_block` event of a job, built inside the dispatch transaction.

    Unscoped: all sections `waiting` (revision of the value they will replace).
    Scoped update (`payload.scope` on the turn's user_message event): sections outside the
    scope are `decided`, copied from the previous job, `changed=false`; sections in scope
    are `deciding`. The caller already holds the thread lock.
    """
    from app.models import CreationThreadEvent  # noqa: PLC0415

    events = load_plan_events(db, thread.id)
    scope: list[str] | None = None
    if source_event_id is not None:
        source = db.get(CreationThreadEvent, source_event_id)
        raw = source.payload.get("scope") if source is not None and source.payload else None
        if isinstance(raw, list):
            scope = [s for s in dict.fromkeys(map(str, raw)) if s in SCOPABLE_SECTIONS] or None
    previous = latest_decided_other_job(events, job_id)
    out: list[dict[str, Any]] = []
    for section in SECTION_ORDER:
        prior = previous.get(section)
        prior_block = prior[1] if prior else None
        revision = block_revision(prior_block)
        if scope and section not in scope and prior_block is not None:
            entry = {
                key: copy.deepcopy(prior_block[key])
                for key in ("summary", "detail", "intent", "skipped", "decided_at", "payload")
                if key in prior_block
            }
            entry.update(section_id=section, state="decided", revision=revision, changed=False)
            entry.setdefault("intent", False)
            entry.setdefault("skipped", False)
            out.append(entry)
            continue
        entry = block(section, "deciding" if scope and section in scope else "waiting")
        entry["revision"] = revision
        entry["changed"] = False
        out.append(entry)
    return plan_block_payload(
        turn_id=turn_id,
        job_id=job_id,
        blocks=out,
        scope=scope,
        previous_job_id=previous_job_id_of(events, job_id),
    )


# ── finalize sweep ───────────────────────────────────────────────────────────


def emit_skipped_remainder(job_id: str | uuid.UUID) -> None:
    """Finalize sweep: every section never decided for this job goes `decided`+`skipped`.

    With the flag on it first (1) enriches sections the render path could only summarise
    from the rendered variant and (2) resolves the post caption; afterwards it appends the
    `plan_update_summary` of a scoped update that changed something.
    """
    if not enabled():
        return
    try:
        _enrich_from_variant(job_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_blocks_enrich_failed", job_id=str(job_id), error=str(exc)[:200])
    emit_post_caption(job_id)
    try:
        from app.models import Job  # noqa: PLC0415

        job_uuid = str(uuid.UUID(str(job_id)))
        decided: set[str] = set()
        seen_any = False
        with sync_session() as db:
            job = db.get(Job, uuid.UUID(job_uuid))
            if job is None or getattr(job, "content_plan_item_id", None) is None:
                return
            thread_id = _thread_id_for(db, job)
            if thread_id is None:
                return
            events = load_plan_events(db, thread_id)
            for payload, entry in iter_blocks(events):
                if payload.get("job_id") != job_uuid:
                    continue
                seen_any = True
                if entry.get("state") == "decided":
                    decided.add(str(entry.get("section_id")))
        if not seen_any:
            return  # the feed never started for this job (flag flipped mid-render, etc.)
        remainder = [
            block(section, "decided", "Not used", skipped=True)
            for section in SECTION_ORDER
            if section not in decided
        ]
        emit_plan_blocks(job_uuid, remainder)
        _emit_update_summary(job_uuid)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_blocks_sweep_failed", job_id=str(job_id), error=str(exc)[:200])


def _enrich_from_variant(job_id: str | uuid.UUID) -> None:
    """Fill sections the cloud (non-guided) path decided without a payload, or never
    decided, from the first rendered variant. Equal-revision re-emits fill in."""
    from app.models import Job  # noqa: PLC0415

    job_uuid = str(uuid.UUID(str(job_id)))
    with sync_session() as db:
        job = db.get(Job, uuid.UUID(job_uuid))
        if job is None or getattr(job, "content_plan_item_id", None) is None:
            return
        thread_id = _thread_id_for(db, job)
        if thread_id is None:
            return
        reduced = reduce_job_blocks(load_plan_events(db, thread_id), job_uuid)
        if not reduced:
            return
        plan = job.assembly_plan if isinstance(job.assembly_plan, dict) else {}
        if isinstance(plan.get("guided_story_execution_plan"), dict):
            return  # guided plans report real payloads stage by stage
        variants = [v for v in plan.get("variants") or [] if isinstance(v, dict)]
        variant = next((v for v in variants if v.get("ok")), variants[0] if variants else None)
        if variant is None:
            return
    todo = [
        section
        for section in SECTION_ORDER[:-1]
        if (
            reduced.get(section) is None
            or reduced[section].get("state") != "decided"
            or (not reduced[section].get("skipped") and reduced[section].get("payload") is None)
        )
    ]
    if not todo:
        return
    payloads = plan_payloads.finalized(plan_payloads.variant_raws(variant))
    out: list[dict[str, Any]] = []
    for section in todo:
        payload = payloads.get(section)
        if payload is None:
            continue
        existing = reduced.get(section)
        summary = (
            existing.get("summary")
            if existing and existing.get("state") == "decided" and not existing.get("skipped")
            else summary_from_payload(section, payload)
        )
        out.append(block(section, "decided", summary, payload=payload))
    if out:
        emit_plan_blocks(job_uuid, out)


def summary_from_payload(section: str, payload: dict[str, Any]) -> str | None:
    if section == "title":
        return str(payload.get("text") or "") or None
    if section == "clips":
        text = _count_label(len(payload.get("clips") or []), "clip")
        total = payload.get("total_duration_s")
        return text + (f" · {round(float(total))}s" if total else "")
    if section == "captions":
        return _count_label(int(payload.get("count") or 0), "caption")
    if section == "music":
        title, artist = payload.get("title"), payload.get("artist")
        if title:
            return f"{title} · {artist}" if artist else str(title)
        return "Your song" if payload.get("source") == "user_song" else "Your voiceover"
    if section == "sfx":
        return _count_label(int(payload.get("count") or 0), "sound effect")
    if section == "overlays":
        return _count_label(int(payload.get("count") or 0), "overlay")
    if section == "look":
        chips = payload.get("chips") or []
        return str(chips[0]) if chips else None
    if section == "post_caption":
        return str(payload.get("text") or "")[:_SUMMARY_MAX] or None
    return None


# ── update summary (scoped update jobs) ──────────────────────────────────────

_SECTION_WORDS = {
    "title": ("title", "başlığı"),
    "clips": ("clips", "klipleri"),
    "captions": ("captions", "altyazıları"),
    "music": ("music", "müziği"),
    "sfx": ("sound effects", "ses efektlerini"),
    "overlays": ("overlays", "katmanları"),
    "look": ("look", "görünümü"),
}


def _join(words: list[str], word: str) -> str:
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} {word} {words[-1]}"


def update_summary_text(changed: list[str]) -> str:
    """Deterministic, localized: composed from the changed sections only."""
    from app.kria.reply_language import say  # noqa: PLC0415

    sections = [s for s in SECTION_ORDER if s in changed and s in _SECTION_WORDS]
    en = _join([_SECTION_WORDS[s][0] for s in sections], "and")
    tr = _join([_SECTION_WORDS[s][1] for s in sections], "ve")
    return say(en=f"Updated {en}.", tr=f"{tr[:1].upper()}{tr[1:]} güncelledim.")[:400]


def _emit_update_summary(job_id: str) -> None:
    """Once per scoped job that changed something; best-effort, never raises."""
    try:
        from app.kria.reply_language import (  # noqa: PLC0415
            reply_language_for,
            thread_reply_language,
        )
        from app.models import CreationThread, CreationThreadEvent, Job  # noqa: PLC0415
        from app.tasks.kria_runtime import _append_sync_event  # noqa: PLC0415

        with sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            thread_id = _thread_id_for(db, job) if job is not None else None
            if thread_id is None:
                return
            thread = db.execute(
                select(CreationThread).where(CreationThread.id == thread_id).with_for_update()
            ).scalar_one_or_none()
            if thread is None:
                return
            events = load_plan_events(db, thread_id)
            if not scope_of(events, job_id):
                return
            reduced = reduce_job_blocks(events, job_id)
            changed = [s for s, b in reduced.items() if b.get("changed") and s in _SECTION_WORDS]
            if not changed:
                return
            existing = db.execute(
                select(CreationThreadEvent.payload).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == SUMMARY_EVENT_TYPE,
                )
            ).scalars()
            if any(isinstance(p, dict) and p.get("job_id") == job_id for p in existing):
                return
            turn_id = next(
                (
                    p.get("turn_id")
                    for p in events
                    if str(p.get("job_id")) == job_id and p.get("turn_id")
                ),
                None,
            )
            with reply_language_for(thread_reply_language(thread)):
                text = update_summary_text(changed)
            _append_sync_event(
                db,
                thread,
                role="system",
                event_type=SUMMARY_EVENT_TYPE,
                content=None,
                payload={
                    "turn_id": turn_id,
                    "job_id": job_id,
                    "text": text,
                    "changed_sections": [s for s in SECTION_ORDER if s in changed],
                },
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_update_summary_failed", job_id=job_id, error=str(exc)[:200])


# ── post caption (KRI-448) ───────────────────────────────────────────────────

_POST_CAPTION_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="post-caption")
_PENDING_COPY: dict[str, tuple[Future, float]] = {}
_PENDING_LOCK = threading.Lock()


def _generate_post_caption(hook_text: str, job_id: str) -> dict[str, Any] | None:
    """`PlatformCopyAgent` -> TikTok caption + hashtags. Raises on any failure (the caller
    turns that into a skipped block); the template-copy fallback is deliberately NOT used."""
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import RunContext  # noqa: PLC0415
    from app.agents.platform_copy import PlatformCopyAgent, PlatformCopyInput  # noqa: PLC0415

    out = PlatformCopyAgent(default_client()).run(
        PlatformCopyInput(hook_text=hook_text[:300], has_transcript=False),
        ctx=RunContext(job_id=job_id),
    )
    tiktok = out.value.tiktok
    return plan_payloads.post_caption_raw(tiktok.caption, tiktok.hashtags)


def _start_post_caption(job_id: str, hook_text: str) -> None:
    with _PENDING_LOCK:
        if job_id in _PENDING_COPY:
            return
        now = time.monotonic()
        for stale in [
            key
            for key, (future, started) in _PENDING_COPY.items()
            if future.done() and now - started > 600
        ]:
            _PENDING_COPY.pop(stale, None)  # a job that never finalized must not leak
        _PENDING_COPY[job_id] = (
            _POST_CAPTION_POOL.submit(_generate_post_caption, hook_text, job_id),
            time.monotonic(),
        )


def _maybe_start_post_caption(job_id: str | uuid.UUID, written: dict[str, Any]) -> None:
    """Start the copy agent in the background as soon as the title is decided, so it runs
    alongside the render. Only generative jobs; never holds a DB lock."""
    if written.get("mode") != "generative" or written.get("scoped"):
        return  # a scoped update copies the previous caption; nothing to generate alongside
    for entry in written["blocks"]:
        if entry["section_id"] == "title" and entry.get("state") == "decided":
            if entry.get("skipped"):
                return
            text = (entry.get("payload") or {}).get("text") or entry.get("summary")
            if text:
                _start_post_caption(str(job_id), str(text))
            return


def _variant_post_caption(job: Any) -> dict[str, Any] | None:
    plan = job.assembly_plan if isinstance(job.assembly_plan, dict) else {}
    candidates: list[Any] = [
        v.get("post_caption") for v in plan.get("variants") or [] if isinstance(v, dict)
    ]
    candidates.append(plan.get("post_caption"))
    guided = plan.get("guided_story_execution_plan")
    if isinstance(guided, dict):
        candidates.append(guided.get("post_caption"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            raw = plan_payloads.post_caption_raw(candidate.get("text"), candidate.get("hashtags"))
            if raw:
                return raw
    return None


def _persist_post_caption(job_id: str, raw: dict[str, Any]) -> None:
    """`variant["post_caption"]` on every variant that lacks one. Job lock only."""
    from app.models import Job  # noqa: PLC0415

    with sync_session() as db:
        job = db.execute(
            select(Job).where(Job.id == uuid.UUID(job_id)).with_for_update()
        ).scalar_one_or_none()
        if job is None or not isinstance(job.assembly_plan, dict):
            return
        plan = copy.deepcopy(job.assembly_plan)
        stored = {"text": raw["text"], "hashtags": raw["hashtags"], "source": "platform_copy"}
        touched = False
        for variant in plan.get("variants") or []:
            if isinstance(variant, dict) and not variant.get("post_caption"):
                variant["post_caption"] = dict(stored)
                touched = True
        if touched:
            job.assembly_plan = plan
            db.commit()


def _drop_pending(job_id: str) -> None:
    with _PENDING_LOCK:
        _PENDING_COPY.pop(job_id, None)


def _await_post_caption(job_id: str, hook_text: str) -> tuple[dict[str, Any] | None, str]:
    with _PENDING_LOCK:
        pending = _PENDING_COPY.get(job_id)
    if pending is None:
        _start_post_caption(job_id, hook_text)
        with _PENDING_LOCK:
            pending = _PENDING_COPY[job_id]
    future, started = pending
    remaining = max(0.5, POST_CAPTION_TIMEOUT_S - (time.monotonic() - started))
    try:
        return future.result(timeout=remaining), "generated"
    except FutureTimeout:
        return None, "timeout"
    except Exception as exc:  # noqa: BLE001 - agent / model failure
        log.info("post_caption_generation_failed", job_id=job_id, error=str(exc)[:200])
        return None, "skipped"
    finally:
        _drop_pending(job_id)


def emit_post_caption(job_id: str | uuid.UUID) -> None:
    """Resolve the `post_caption` block for a job: use the variant's copy, else generate
    it (8 s, best effort), else `decided`+`skipped`. Never raises, holds no DB lock while
    waiting. A scoped update copies the block, so it is already decided and this is a no-op."""
    if not enabled():
        return
    key = str(job_id)
    try:
        from app.models import CreationThread, Job  # noqa: PLC0415

        with sync_session() as db:
            job = db.get(Job, uuid.UUID(key))
            if job is None or job.status == "cancelled":
                return
            thread_id = _thread_id_for(db, job)
            if thread_id is None:
                return
            reduced = reduce_job_blocks(load_plan_events(db, thread_id), key)
            if (reduced.get("post_caption") or {}).get("state") == "decided":
                _drop_pending(key)
                return
            if not reduced:
                return  # the feed never started for this job
            raw = _variant_post_caption(job)
            mode = getattr(job, "mode", None)
            title = reduced.get("title") or {}
            hook = (title.get("payload") or {}).get("text") or (
                title.get("summary") if not title.get("skipped") else None
            )
            if not hook and mode == "generative":
                state = db.execute(
                    select(CreationThread.state).where(CreationThread.id == thread_id)
                ).scalar_one_or_none()
                hook = str((state or {}).get("intent") or "")[:200] or None
        outcome = "from_variant" if raw else "skipped"
        if raw is None and mode == "generative" and hook:
            raw, outcome = _await_post_caption(key, str(hook))
            if raw is not None:
                try:
                    _persist_post_caption(key, raw)
                except Exception as exc:  # noqa: BLE001 - the block still reports it
                    log.warning("post_caption_persist_failed", job_id=key, error=str(exc)[:200])
        payload = plan_payloads.finalize_payload("post_caption", raw)
        log.info(
            "plan_post_caption", outcome=outcome if payload or outcome == "timeout" else "skipped"
        )
        emit_plan_blocks(
            key,
            [
                block(
                    "post_caption",
                    "decided",
                    summary_from_payload("post_caption", payload),
                    payload=payload,
                )
                if payload
                else block("post_caption", "decided", "Not used", skipped=True)
            ],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_post_caption_failed", job_id=key, error=str(exc)[:200])


# ── block builders (summary exactly as before; payload added behind the flag) ─


def _count_label(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _skipped(section: str) -> dict[str, Any]:
    return block(section, "decided", "Not used", skipped=True)


def _caption_count(plan: dict[str, Any]) -> int:
    count = 0
    for field in ("narration_label_text_elements", "context_label_text_elements"):
        rows = plan.get(field) or []
        if isinstance(rows, list):
            count += sum(1 for row in rows if isinstance(row, dict))
    return count


def _music_block(plan: dict[str, Any]) -> dict[str, Any]:
    music = plan.get("music")
    if isinstance(music, dict) and music.get("title"):
        artist = music.get("artist")
        return block(
            "music", "decided", f"{music['title']} · {artist}" if artist else music["title"]
        )
    song = plan.get("user_song")
    if isinstance(song, dict):
        lipsync = song.get("mode") == "lipsync"
        return block("music", "decided", "Your song", "Lip-sync" if lipsync else "Background")
    if plan.get("narration"):
        return block("music", "decided", "Your voiceover")
    return _skipped("music")


def _summary_block(plan: dict[str, Any], section: str) -> dict[str, Any]:
    if section == "title":
        # A per-clip text is never the title: a chapter-titled montage with no opening title
        # must not report its first chapter name ("Sabah") as one (KRI-545). Same prefixes as
        # guided_story's `_GUIDED_CLIP_TEXT_PREFIXES`.
        texts = [
            t
            for t in (plan.get("text_elements") or [])
            if isinstance(t, dict)
            and t.get("text")
            and not str(t.get("id") or "").startswith(("clip-label-", "montage-text-"))
        ]
        return block("title", "decided", str(texts[0]["text"])) if texts else _skipped("title")
    if section == "clips":
        timeline = plan.get("story_timeline")
        duration = plan.get("resolved_duration_s")
        if isinstance(timeline, list) and timeline:
            summary = _count_label(len(timeline), "clip")
            if isinstance(duration, int | float) and duration > 0:
                summary += f" · {round(float(duration))}s"
            return block("clips", "decided", summary)
        return _skipped("clips")
    if section == "captions":
        count = _caption_count(plan)
        return (
            block("captions", "decided", _count_label(count, "caption"))
            if count
            else _skipped("captions")
        )
    if section == "music":
        return _music_block(plan)
    if section == "sfx":
        sfx = plan.get("editor_sound_effects") or []
        if isinstance(sfx, list) and sfx:
            return block("sfx", "decided", _count_label(len(sfx), "sound effect"))
        return _skipped("sfx")
    if section == "overlays":
        overlays = plan.get("editor_media_overlays") or []
        if isinstance(overlays, list) and overlays:
            return block("overlays", "decided", _count_label(len(overlays), "overlay"))
        return _skipped("overlays")
    if section == "look":
        typography = plan.get("typography")
        style_id = typography.get("style_id") if isinstance(typography, dict) else None
        if style_id:
            return block("look", "decided", str(style_id).replace("_", " ").capitalize())
        return _skipped("look")
    if section == "post_caption":
        caption = plan.get("post_caption")
        text = caption.get("text") if isinstance(caption, dict) else None
        return block("post_caption", "decided", str(text)) if text else _skipped("post_caption")
    raise ValueError(f"unknown plan block section: {section}")


def with_payload(entry: dict[str, Any], payload: dict[str, Any] | None) -> dict[str, Any]:
    """Attach a validated payload to a decided, non-skipped block (else unchanged)."""
    if payload is not None and entry["state"] == "decided" and not entry["skipped"]:
        entry["payload"] = payload
    return entry


def decided_block(plan: dict[str, Any], section: str, *, track_meta: Any = None) -> dict[str, Any]:
    """One section `decided` from a pinned guided plan. Only fields the plan carries."""
    entry = _summary_block(plan, section)
    if enabled() and not entry["skipped"]:
        raw = plan_payloads.guided_section_raw(plan, section, track_meta)
        with_payload(entry, plan_payloads.finalize_payload(section, raw))
    return entry


def pending_post_caption(plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """`post_caption` is `deciding` until `emit_post_caption` resolves it (flag on)."""
    if plan is not None and isinstance(plan.get("post_caption"), dict):
        return decided_block(plan, "post_caption")
    if enabled():
        return block("post_caption", "deciding")
    return _skipped("post_caption")


def blocks_from_guided_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Every section from a pinned guided execution plan (phone path: the render happens on
    the device, so the cloud decisions are the whole story). `post_caption` stays
    `deciding` for `emit_post_caption` unless the plan already carries one."""
    return [
        pending_post_caption(plan) if section == "post_caption" else decided_block(plan, section)
        for section in SECTION_ORDER
    ]


def _track_clips(recipe: Any, *, kind: str | None = None, track_id: str | None = None) -> list[Any]:
    clips: list[Any] = []
    for track in getattr(recipe, "tracks", None) or []:
        if kind is not None and getattr(track, "kind", None) != kind:
            continue
        if track_id is not None and getattr(track, "id", None) != track_id:
            continue
        clips.extend(getattr(track, "clips", None) or [])
    return clips


def blocks_from_phone_recipe(
    recipe: Any,
    *,
    title: str | None = None,
    title_bar_id: str | None = None,
    clip_count: int | None = None,
    captions: int | list[dict[str, Any]] = 0,
    music: str | None = None,
    music_detail: str | None = None,
    music_track_id: str | None = None,
    look: str | None = None,
) -> list[dict[str, Any]]:
    """All sections for a phone recipe pinned by the narrated, voiceover-montage or
    subtitled writers (the render happens on the device, so these decisions are the whole
    story). `post_caption` is `deciding` until `emit_post_caption` resolves it.

    The recipe supplies what it truly carries: clip count (distinct sources on the
    video track unless `clip_count` overrides it) and duration, sound-effect clips
    (the `sfx` track) and overlay clips (overlay-kind tracks other than the Talking
    cutaways, which count as clips). `title`, `captions`, `music` and `look` come from
    the caller's own decision; a falsy value means that section was not used. `captions`
    is a count, or the cue dicts (then the payload lists the lines).
    """
    cutaway_track_id = "talking-head-cutaways"
    caption_cues = captions if isinstance(captions, list) else None
    caption_count = len(captions) if isinstance(captions, list) else int(captions or 0)
    if clip_count is None:
        clip_count = len(
            {getattr(clip, "source_asset_id", None) for clip in _track_clips(recipe, kind="video")}
            - {None}
        )
    duration = getattr(recipe, "duration", None)
    by_section: dict[str, dict[str, Any]] = {}
    clean_title = " ".join(str(title or "").split())
    by_section["title"] = (
        block("title", "decided", clean_title) if clean_title else _skipped("title")
    )
    if clip_count > 0:
        summary = _count_label(clip_count, "clip")
        if isinstance(duration, int | float) and duration > 0:
            summary += f" · {round(float(duration))}s"
        by_section["clips"] = block("clips", "decided", summary)
    else:
        by_section["clips"] = _skipped("clips")
    by_section["captions"] = (
        block("captions", "decided", _count_label(caption_count, "caption"))
        if caption_count > 0
        else _skipped("captions")
    )
    by_section["music"] = (
        block("music", "decided", music, music_detail) if music else _skipped("music")
    )
    sfx = len(_track_clips(recipe, track_id="sfx"))
    by_section["sfx"] = (
        block("sfx", "decided", _count_label(sfx, "sound effect")) if sfx else _skipped("sfx")
    )
    overlays = [
        clip
        for track in getattr(recipe, "tracks", None) or []
        if getattr(track, "kind", None) == "overlay"
        and getattr(track, "id", None) != cutaway_track_id
        for clip in getattr(track, "clips", None) or []
    ]
    by_section["overlays"] = (
        block("overlays", "decided", _count_label(len(overlays), "overlay"))
        if overlays
        else _skipped("overlays")
    )
    clean_look = str(look or "").strip()
    by_section["look"] = (
        block("look", "decided", clean_look.replace("_", " ").capitalize())
        if clean_look
        else _skipped("look")
    )
    by_section["post_caption"] = pending_post_caption()
    if enabled():
        _attach_recipe_payloads(
            by_section,
            recipe,
            title=clean_title,
            title_bar_id=title_bar_id,
            clip_count=clip_count,
            caption_cues=caption_cues,
            music=music,
            music_track_id=music_track_id,
            look=clean_look,
        )
    return [by_section[section] for section in SECTION_ORDER]


def _attach_recipe_payloads(
    by_section: dict[str, dict[str, Any]],
    recipe: Any,
    *,
    title: str,
    title_bar_id: str | None,
    clip_count: int,
    caption_cues: list[dict[str, Any]] | None,
    music: str | None,
    music_track_id: str | None,
    look: str,
) -> None:
    """Best-effort: a payload that cannot be built leaves the block summary-only."""
    try:
        raws: dict[str, dict[str, Any] | None] = {}
        raws["title"] = plan_payloads.title_raw(title, title_bar_id) if title else None
        clips = plan_payloads.recipe_clips_raw(recipe)
        if clips is not None and len(clips["clips"]) != clip_count:
            clips = None  # the caller's count (e.g. cutaways) disagrees with the video track
        raws["clips"] = clips
        lines, count = plan_payloads.caption_lines(caption_cues)
        raws["captions"] = plan_payloads.captions_raw(lines, count)
        if music:
            audio = getattr(recipe, "audio", None)
            mix = plan_payloads.mix_levels(
                plan_payloads.to_float(getattr(audio, "music_volume", None))
                if music_track_id
                else None,
                plan_payloads.to_float(getattr(audio, "original_volume", None)),
            )
            if music_track_id:
                meta = plan_payloads.lookup_track_meta(music_track_id) or {}
                raws["music"] = {
                    "source": "catalog",
                    "track_id": music_track_id,
                    "title": meta.get("title") or music.split(" · ")[0],
                    "artist": meta.get("artist"),
                    "bpm": meta.get("bpm"),
                    "mix": mix,
                }
            else:
                raws["music"] = {"source": "voiceover", "mix": mix}
        raws["sfx"], raws["overlays"] = plan_payloads.recipe_sfx_overlays(recipe)
        raws["look"] = plan_payloads.look_raw(style_id=look) if look else None
        for section, raw in raws.items():
            with_payload(by_section[section], plan_payloads.finalize_payload(section, raw))
    except Exception as exc:  # noqa: BLE001 - payloads are decoration over the summary
        log.info("plan_recipe_payloads_failed", error=str(exc)[:200])


def emit_phone_recipe_blocks(job_id: str | uuid.UUID, recipe: Any, **facts: Any) -> None:
    """Build `blocks_from_phone_recipe` and emit them, then resolve the post caption.
    Best-effort; never raises.

    Call it from the render thread AFTER the device request is pinned and committed,
    with no Job/PlanItem/Plan lock held (same discipline as the guided path).
    """
    if not enabled():
        return
    try:
        emit_plan_blocks(job_id, blocks_from_phone_recipe(recipe, **facts))
        emit_post_caption(job_id)
    except Exception as exc:  # noqa: BLE001 - the feed must never fail a render
        log.warning("plan_blocks_phone_failed", job_id=str(job_id), error=str(exc)[:200])


def cloud_decision_blocks(
    agent_text: Any, style_set_id: Any, best_track: Any
) -> list[dict[str, Any]]:
    """`title` / `look` / `music` decided for the cloud (non-guided) generative path, after
    the text, style and song-match join. Summaries are exactly what the path always sent."""
    title = (
        block("title", "decided", str(agent_text))
        if agent_text
        else block("title", "decided", "Not used", skipped=True)
    )
    look = (
        block("look", "decided", str(style_set_id).replace("_", " ").capitalize())
        if style_set_id
        else block("look", "decided", "Not used", skipped=True)
    )
    if best_track is not None and getattr(best_track, "title", None):
        artist = getattr(best_track, "artist", None)
        music = block(
            "music",
            "decided",
            f"{best_track.title} \u00b7 {artist}" if artist else str(best_track.title),
        )
    else:
        music = block("music", "decided", "Not used", skipped=True)
    if enabled():
        try:
            with_payload(
                title,
                plan_payloads.finalize_payload("title", plan_payloads.title_raw(agent_text)),
            )
            with_payload(
                look,
                plan_payloads.finalize_payload(
                    "look", plan_payloads.look_raw(style_id=style_set_id)
                ),
            )
            if not music["skipped"]:
                track_id = getattr(best_track, "id", None)
                meta = (plan_payloads.lookup_track_meta(str(track_id)) if track_id else None) or {}
                with_payload(
                    music,
                    plan_payloads.finalize_payload(
                        "music",
                        {
                            "source": "catalog",
                            "track_id": str(track_id) if track_id else None,
                            "title": str(best_track.title),
                            "artist": getattr(best_track, "artist", None) or meta.get("artist"),
                            "bpm": meta.get("bpm"),
                        },
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - payloads are decoration
            log.info("plan_cloud_payloads_failed", error=str(exc)[:200])
    return [title, look, music]


def deciding_blocks(sections: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    return [block(section, "deciding") for section in sections]


def make_stage_reporter(job_id: str | uuid.UUID, plan: dict[str, Any]):
    """Callback for the real render stages: `report(sections, state)`.

    `state="deciding"` marks work started; `"decided"` reports the plan's real values
    (or `Not used` when the plan truly lacks them). Best-effort; never raises. Call it
    only from the render thread with no Job/PlanItem/Plan lock held.
    """

    def report(sections: tuple[str, ...] | list[str], state: str) -> None:
        if not enabled():
            return
        try:
            blocks = (
                deciding_blocks(sections)
                if state == "deciding"
                else [decided_block(plan, section) for section in sections]
            )
            emit_plan_blocks(job_id, blocks)
        except Exception as exc:  # noqa: BLE001
            log.warning("plan_blocks_stage_failed", error=str(exc)[:200])

    return report
