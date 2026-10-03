"""Analyse a runtime-v2 thread's clips so the copilot knows what the footage shows.

The phone attach flow stores only capture/place facts on ``clip_assignments``; the
vision analyzer (`creator_clip_analysis.analyze_clip_assignment`) never ran for these
clips, so `analysis["understanding"]` stayed empty and chat edits could not caption
"what is happening". `attach_media` enqueues this task after its commit.

Contract: background, idempotent, NEVER fatal. It never blocks attach, a render or a
chat turn (a turn that needs the description before it lands simply clarifies); a clip
that already has understanding is skipped; a failure is logged and the next clip still
runs; a provider quota/budget stop ends the run without retries. Cost: one Gemini vision
call per clip on the small analysis proxy (about $0.01-0.03 each). Kill switch:
``KRIA_CLIP_UNDERSTANDING_ENABLED=false`` + worker restart.
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import structlog
from sqlalchemy import select

from app.agents._runtime import (
    AiBudgetExceededError,
    ProviderOutcomeUnknownError,
    ProviderQuotaExceededError,
    RunContext,
)
from app.config import settings
from app.database import sync_session
from app.models import ContentPlan, PlanItem
from app.services.clip_understanding import (
    UNDERSTANDING_ATTEMPTS_KEY,
    UNDERSTANDING_MAX_ATTEMPTS,
    clip_record,
    understanding_attempts,
    understanding_incomplete,
)
from app.worker import celery_app

log = structlog.get_logger()
# KRI-282 L2: one run now loops chunks until every clip has a record (or a bounded
# stop), instead of silently analysing only the first 24 until a later attach.
CHUNK_SIZE = 12
# Parallel per-clip Gemini calls. The process-wide Gemini invoke pool is 8 slots shared
# with every other agent, so stay well under it.
CONCURRENCY = 3
# Per-run wall clock, strictly under soft_time_limit (1500) < time_limit (1560) < the
# broker visibility_timeout; a run that hits it re-enqueues itself.
RUN_DEADLINE_S = 1100.0
MAX_CHAIN = 8  # bound on self re-enqueues per attach burst (quota / deadline resumes)
QUOTA_RESUME_DELAY_S = 300
LOCK_TTL_S = 1560


def _needs_understanding(row: object) -> bool:
    return (
        isinstance(row, dict)
        and bool(row.get("gcs_path"))
        and bool(row.get("media_id"))
        and understanding_incomplete(row.get("analysis"), kind=str(row.get("kind") or "video"))
    )


def _enabled() -> bool:
    return bool(settings.kria_clip_understanding_enabled and settings.gemini_api_key)


def _candidates(item_id: uuid.UUID) -> tuple[uuid.UUID, list[dict[str, Any]]] | None:
    with sync_session() as db:
        item = db.get(PlanItem, item_id)
        plan = db.get(ContentPlan, item.content_plan_id) if item is not None else None
        if item is None or plan is None:
            return None
        rows = [
            dict(row)
            for row in (item.clip_assignments or [])
            if _needs_understanding(row) and row.get("kind", "video") in {"video", "image"}
        ]
        return plan.user_id, rows


def _store(item_id: uuid.UUID, entry: dict[str, Any]) -> bool:
    """Merge one clip's analysis into the live assignment (identity + generation fenced)."""
    from app.services.plan_item_media import (  # noqa: PLC0415
        current_detector_policy,
        mutate_plan_item_media,
    )
    from app.services.speech_cleanup_preflight import (  # noqa: PLC0415
        mutation_current_analysis_sync,
    )

    with sync_session() as db:
        item = db.execute(
            select(PlanItem).where(PlanItem.id == item_id).with_for_update()
        ).scalar_one_or_none()
        if item is None:
            return False
        rows: list[Any] = []
        changed = False
        for row in item.clip_assignments or []:
            if (
                isinstance(row, dict)
                and str(row.get("media_id")) == str(entry.get("media_id"))
                and str(row.get("gcs_path")) == str(entry.get("gcs_path"))
                and _needs_understanding(row)
            ):
                stored_generation = str(row.get("storage_generation") or "")
                if stored_generation and stored_generation != str(entry.get("generation") or ""):
                    rows.append(row)  # the object was replaced while we analysed it
                    continue
                merged = {**dict(row.get("analysis") or {}), **dict(entry.get("analysis") or {})}
                rows.append({**row, "analysis": merged})
                changed = True
            else:
                rows.append(row)
        if not changed:
            return False
        result = mutate_plan_item_media(
            item,
            detector_policy=current_detector_policy(),
            clip_assignments=rows,
            current_analysis=mutation_current_analysis_sync(db, item.id, for_update=True),
        )
        if result.source_changed:
            # An analysis-only write must never change footage identity; undo it all.
            db.rollback()
            return False
        db.commit()
        return True


def _with_speech_segments(entry: dict[str, Any]) -> dict[str, Any]:
    """KRI-282: add timed sentence segments to a clip that has speech. Best effort.

    Uses the cached whisper path on the same analysis proxy the vision analyzer
    just read. Any failure leaves the entry exactly as analysed, so this can
    never turn a successful analysis into a failed one. Kill switch:
    ``SPEECH_EXCERPT_MONTAGE_ENABLED=false`` (nothing is transcribed or stored).
    """
    if not settings.speech_excerpt_montage_enabled:
        return entry
    try:
        analysis = entry.get("analysis")
        if str(entry.get("kind") or "video") != "video" or not isinstance(analysis, dict):
            return entry
        speech = clip_record(analysis, kind="video").speech
        if not speech.has_speech or speech.segments:
            return entry
        from app.services.speech_segments import (  # noqa: PLC0415
            attach_segments_to_analysis,
            transcribe_stored_clip,
        )

        words, language = transcribe_stored_clip(str(entry["gcs_path"]))
        return {**entry, "analysis": attach_segments_to_analysis(analysis, words, language)}
    except Exception:  # noqa: BLE001 - segments are an enhancement, never a reason to fail
        log.warning(
            "kria_clip_speech_segments_failed",
            media_id=str(entry.get("media_id")),
            exc_info=True,
        )
        return entry


class _Stop(Exception):  # noqa: N818 - internal control flow
    """Provider quota/budget/unknown-billing stop: end the run, resume later."""


def _analyze_one(raw: dict[str, Any], *, user_id: uuid.UUID, item_id: str) -> dict[str, Any]:
    """One clip, retried once on a transient failure. Runs in a worker thread."""
    from app.services.creator_clip_analysis import analyze_clip_assignment  # noqa: PLC0415

    last: Exception | None = None
    for _attempt in range(2):
        try:
            entry, _ref = analyze_clip_assignment(
                raw,
                {},
                run_context=RunContext(
                    creator_id=str(user_id),
                    request_id=f"kria-clip:{item_id}:{raw['media_id']}",
                    usage_purpose="optional_background",
                ),
                require_semantic=True,
            )
            return _with_speech_segments(entry)
        except (AiBudgetExceededError, ProviderQuotaExceededError, ProviderOutcomeUnknownError):
            raise _Stop from None
        except Exception as exc:  # noqa: BLE001 - one bad clip must not stop the rest
            last = exc
    assert last is not None
    raise last


def _settle_empty(entry: dict[str, Any]) -> dict[str, Any]:
    """A successful analysis that still yields no record is final: stop re-asking."""
    analysis = dict(entry.get("analysis") or {})
    if clip_record(analysis, kind=str(entry.get("kind") or "video")).is_empty():
        analysis[UNDERSTANDING_ATTEMPTS_KEY] = UNDERSTANDING_MAX_ATTEMPTS
        return {**entry, "analysis": analysis}
    return entry


def _record_failure(item_id: uuid.UUID, raw: dict[str, Any]) -> None:
    """Count a failed attempt on the clip so a persistent failure settles, not loops."""
    try:
        _store(
            item_id,
            {
                "media_id": raw.get("media_id"),
                "gcs_path": raw.get("gcs_path"),
                "generation": raw.get("storage_generation") or "",
                "analysis": {
                    UNDERSTANDING_ATTEMPTS_KEY: understanding_attempts(raw.get("analysis")) + 1
                },
            },
        )
    except Exception:  # noqa: BLE001 - bookkeeping only
        log.warning("kria_clip_understanding_attempt_record_failed", exc_info=True)


def _lock_key(item_id: str) -> str:
    return f"kria-clip-und:{item_id}"


def _acquire_lock(item_id: str) -> tuple[Any, str] | None | bool:
    """Per-item single-flight so a 47-attach burst runs ONE analysis loop, not 47.

    Returns (client, token) when held, ``False`` when another run holds it, and
    ``None`` (fail open) when Redis is unavailable.
    """
    try:
        import redis  # noqa: PLC0415

        client = redis.from_url(settings.redis_url)
        token = uuid.uuid4().hex
        if client.set(_lock_key(item_id), token, nx=True, ex=LOCK_TTL_S):
            return client, token
        return False
    except Exception:  # noqa: BLE001
        return None


def _release_lock(item_id: str, held: tuple[Any, str] | None | bool) -> None:
    if not isinstance(held, tuple):
        return
    client, token = held
    try:
        current = client.get(_lock_key(item_id))
        if isinstance(current, bytes):
            current = current.decode()
        if current == token:
            client.delete(_lock_key(item_id))
    except Exception:  # noqa: BLE001
        pass


def _resume(item_id: str, chain: int, *, countdown: int) -> bool:
    if chain >= MAX_CHAIN:
        log.warning("kria_clip_understanding_chain_exhausted", item_id=item_id, chain=chain)
        return False
    try:
        analyze_kria_clips.apply_async(
            args=[item_id, chain + 1],
            countdown=countdown,
            queue=settings.pool_asset_analysis_queue,
            expires=3600,
        )
        return True
    except Exception:  # noqa: BLE001
        log.warning("kria_clip_understanding_resume_failed", item_id=item_id, exc_info=True)
        return False


@celery_app.task(
    bind=True,
    name="tasks.analyze_kria_clips",
    soft_time_limit=1500,
    time_limit=1560,
    max_retries=0,
)
def analyze_kria_clips(self, item_id: str, chain: int = 0) -> dict[str, Any]:  # noqa: ANN001
    """Analyse EVERY clip of one plan item that has no stored understanding yet.

    Loops chunks (bounded concurrency) until none remain, the wall-clock deadline hits
    (self re-enqueue), or a provider quota/budget stop (re-enqueue after a delay, up to
    ``MAX_CHAIN``) -- never a silent end with clips left blank.
    """
    if not _enabled():
        return {"item_id": item_id, "status": "disabled"}
    held = _acquire_lock(item_id)
    if held is False:
        return {"item_id": item_id, "status": "busy"}  # the running loop re-reads each chunk
    result: dict[str, Any] = {"item_id": item_id, "status": "failed"}
    try:
        result = _run(item_id, chain)
    finally:
        _release_lock(item_id, held)
    if result.get("status") == "done" and not result.get("resumed"):
        # A clip attached after the loop's last read must not wait for the next attach.
        try:
            found = _candidates(uuid.UUID(item_id))
        except Exception:  # noqa: BLE001
            found = None
        if found and found[1] and not result.get("stopped"):
            _resume(item_id, chain, countdown=5)
    return result


def _run(item_id: str, chain: int) -> dict[str, Any]:
    identifier = uuid.UUID(item_id)
    started = time.monotonic()
    done = failed = 0
    stopped = resumed = False
    while True:
        try:
            found = _candidates(identifier)
        except Exception:  # noqa: BLE001 - best-effort background work
            log.warning("kria_clip_understanding_load_failed", item_id=item_id, exc_info=True)
            return {"item_id": item_id, "status": "failed"}
        if found is None:
            return {"item_id": item_id, "status": "ignored"}
        user_id, rows = found
        if not rows:
            break
        if time.monotonic() - started > RUN_DEADLINE_S:
            resumed = _resume(item_id, chain, countdown=5)
            break
        progressed = False
        with ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="kria-clip") as pool:
            futures = {
                pool.submit(_analyze_one, raw, user_id=user_id, item_id=item_id): raw
                for raw in rows[:CHUNK_SIZE]
            }
            for future in as_completed(futures):
                raw = futures[future]
                if future.cancelled():
                    continue
                try:
                    entry = _settle_empty(future.result())
                    if _store(identifier, entry):
                        done += 1
                        progressed = True
                except _Stop:
                    stopped = True
                    for pending in futures:
                        pending.cancel()
                except Exception:  # noqa: BLE001
                    failed += 1
                    progressed = True
                    _record_failure(identifier, raw)
                    log.warning(
                        "kria_clip_understanding_clip_failed",
                        item_id=item_id,
                        media_id=str(raw.get("media_id")),
                        exc_info=True,
                    )
        if stopped:
            # Budget/quota/unknown billing: do not hammer the provider now, but do NOT
            # leave the clips blank either -- resume later (bounded by MAX_CHAIN).
            log.warning("kria_clip_understanding_stopped", item_id=item_id, chain=chain)
            resumed = _resume(item_id, chain, countdown=QUOTA_RESUME_DELAY_S)
            break
        if not progressed:
            break  # every store was fenced (object replaced): avoid a hot loop
    log.info(
        "kria_clip_understanding_done",
        item_id=item_id,
        analyzed=done,
        failed=failed,
        stopped=stopped,
        chain=chain,
    )
    return {
        "item_id": item_id,
        "status": "done",
        "analyzed": done,
        "failed": failed,
        "stopped": stopped,
        "resumed": resumed,
    }


def enqueue_clip_understanding(item_id: uuid.UUID) -> None:
    """Fire-and-forget; never raises (the caller already committed its own work)."""
    if not _enabled():
        return
    try:
        analyze_kria_clips.apply_async(
            args=[str(item_id)], queue=settings.pool_asset_analysis_queue, expires=3600
        )
    except Exception:  # noqa: BLE001
        log.warning("kria_clip_understanding_enqueue_failed", item_id=str(item_id), exc_info=True)
