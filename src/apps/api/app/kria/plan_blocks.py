"""Live plan block feed (KRI-443): `plan_block` events on a v2 creation thread.

After Create, the render pipeline reports what it decided, section by section.
iOS reads these through the existing `GET /creation-threads/{id}/delta` poll.
Contract: docs/pipelines/live-plan-blocks.md.

Invariants:
* Dark behind `LIVE_PLAN_REVIEW_ENABLED`; off = no event, no query, no side effect.
* `emit_plan_blocks` is best-effort and NEVER raises: a feed problem must not
  fail a render.
* It opens its OWN short session and locks only the CreationThread row (last in
  `app/db_locks.CANONICAL_LOCK_ORDER`). Never call it while the caller holds a
  Job/PlanItem/Plan row lock (the `record_pipeline_event` lock trap), and call it
  from the main render thread, after the surrounding `db.commit()`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from app.config import settings
from app.database import sync_session

log = structlog.get_logger()

SECTION_ORDER: tuple[str, ...] = (
    "title",
    "clips",
    "captions",
    "music",
    "sfx",
    "overlays",
    "look",
)
STATES = ("waiting", "deciding", "decided")
EVENT_TYPE = "plan_block"
_SUMMARY_MAX = 120
_DETAIL_MAX = 400


def block(
    section_id: str,
    state: str,
    summary: str | None = None,
    detail: str | None = None,
    *,
    intent: bool = False,
    skipped: bool = False,
) -> dict[str, Any]:
    """One contract block. `decided_at` is stamped only for `decided`."""
    if section_id not in SECTION_ORDER:
        raise ValueError(f"unknown plan block section: {section_id}")
    if state not in STATES:
        raise ValueError(f"unknown plan block state: {state}")
    return {
        "section_id": section_id,
        "state": state,
        "summary": (summary or None) and str(summary)[:_SUMMARY_MAX],
        "detail": (detail or None) and str(detail)[:_DETAIL_MAX],
        "intent": bool(intent),
        "skipped": bool(skipped),
        "decided_at": datetime.now(UTC).isoformat() if state == "decided" else None,
    }


def waiting_blocks() -> list[dict[str, Any]]:
    return [block(section, "waiting") for section in SECTION_ORDER]


def plan_block_payload(
    *, turn_id: str | None, job_id: str, blocks: list[dict[str, Any]]
) -> dict[str, Any]:
    return {"turn_id": turn_id, "job_id": str(job_id), "blocks": blocks}


def enabled() -> bool:
    return bool(settings.live_plan_review_enabled)


def emit_plan_blocks(job_id: str | uuid.UUID, blocks: list[dict[str, Any]]) -> None:
    """Append one `plan_block` event for `job_id`'s thread. Best-effort, never raises."""
    if not enabled() or not blocks:
        return
    try:
        _emit(job_id, blocks)
    except Exception as exc:  # noqa: BLE001 - the feed must never fail a render
        log.warning(
            "plan_blocks_emit_failed",
            job_id=str(job_id),
            error_class=type(exc).__name__,
            error=str(exc)[:200],
        )


def _emit(job_id: str | uuid.UUID, blocks: list[dict[str, Any]]) -> None:
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
            return
        item_id = getattr(job, "content_plan_item_id", None)
        if item_id is None:
            return
        thread_id = db.execute(
            select(CreationThread.id)
            .where(
                CreationThread.active_plan_item_id == item_id,
                CreationThread.creator_id == job.user_id,
                CreationThread.runtime_version == 2,
            )
            .limit(1)
        ).scalar_one_or_none()
        if thread_id is None:
            return
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
            return
        _append_sync_event(
            db,
            thread,
            role="system",
            event_type=EVENT_TYPE,
            content=None,
            payload=plan_block_payload(
                turn_id=str(turn_id) if turn_id else None,
                job_id=str(job_uuid),
                blocks=blocks,
            ),
        )
        db.commit()


def emit_skipped_remainder(job_id: str | uuid.UUID) -> None:
    """Finalize sweep: every section never decided for this job goes `decided`+`skipped`."""
    if not enabled():
        return
    try:
        from app.models import CreationThreadEvent  # noqa: PLC0415

        job_uuid = str(uuid.UUID(str(job_id)))
        decided: set[str] = set()
        seen_any = False
        with sync_session() as db:
            from app.models import CreationThread, Job  # noqa: PLC0415

            job = db.get(Job, uuid.UUID(job_uuid))
            item_id = getattr(job, "content_plan_item_id", None)
            if job is None or item_id is None:
                return
            thread_id = db.execute(
                select(CreationThread.id)
                .where(
                    CreationThread.active_plan_item_id == item_id,
                    CreationThread.creator_id == job.user_id,
                    CreationThread.runtime_version == 2,
                )
                .limit(1)
            ).scalar_one_or_none()
            if thread_id is None:
                return
            rows = db.execute(
                select(CreationThreadEvent.payload).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == EVENT_TYPE,
                )
            ).scalars()
            for payload in rows:
                if not isinstance(payload, dict) or payload.get("job_id") != job_uuid:
                    continue
                seen_any = True
                for entry in payload.get("blocks") or []:
                    if isinstance(entry, dict) and entry.get("state") == "decided":
                        decided.add(str(entry.get("section_id")))
        if not seen_any:
            return  # the feed never started for this job (flag flipped mid-render, etc.)
        remainder = [
            block(section, "decided", "Not used", skipped=True)
            for section in SECTION_ORDER
            if section not in decided
        ]
        emit_plan_blocks(job_uuid, remainder)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_blocks_sweep_failed", job_id=str(job_id), error=str(exc)[:200])


def _count_label(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def blocks_from_guided_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """All seven sections `decided` from a pinned guided execution plan.

    Values come only from fields the plan actually carries; a section with nothing
    in the plan is `decided` + `skipped` ("Not used"). Nothing is invented.
    """
    out: list[dict[str, Any]] = []

    texts = [t for t in (plan.get("text_elements") or []) if isinstance(t, dict) and t.get("text")]
    out.append(
        block("title", "decided", str(texts[0]["text"]))
        if texts
        else block("title", "decided", "Not used", skipped=True)
    )

    timeline = plan.get("story_timeline")
    duration = plan.get("resolved_duration_s")
    if isinstance(timeline, list) and timeline:
        summary = _count_label(len(timeline), "clip")
        if isinstance(duration, int | float) and duration > 0:
            summary += f" · {round(float(duration))}s"
        out.append(block("clips", "decided", summary))
    else:
        out.append(block("clips", "decided", "Not used", skipped=True))

    labels = plan.get("narration_label_text_elements") or []
    if isinstance(labels, list) and labels:
        out.append(block("captions", "decided", _count_label(len(labels), "caption")))
    else:
        out.append(block("captions", "decided", "Not used", skipped=True))

    music = plan.get("music")
    if isinstance(music, dict) and music.get("title"):
        artist = music.get("artist")
        out.append(
            block("music", "decided", f"{music['title']} · {artist}" if artist else music["title"])
        )
    else:
        out.append(block("music", "decided", "Not used", skipped=True))

    sfx = plan.get("editor_sound_effects") or []
    if isinstance(sfx, list) and sfx:
        out.append(block("sfx", "decided", _count_label(len(sfx), "sound effect")))
    else:
        out.append(block("sfx", "decided", "Not used", skipped=True))

    overlays = plan.get("editor_media_overlays") or []
    if isinstance(overlays, list) and overlays:
        out.append(block("overlays", "decided", _count_label(len(overlays), "overlay")))
    else:
        out.append(block("overlays", "decided", "Not used", skipped=True))

    typography = plan.get("typography")
    style_id = typography.get("style_id") if isinstance(typography, dict) else None
    if style_id:
        out.append(block("look", "decided", str(style_id).replace("_", " ").capitalize()))
    else:
        out.append(block("look", "decided", "Not used", skipped=True))
    return out
