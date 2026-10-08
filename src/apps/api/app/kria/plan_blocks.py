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
            "music", "decided", f"{music['title']} \u00b7 {artist}" if artist else music["title"]
        )
    song = plan.get("user_song")
    if isinstance(song, dict):
        lipsync = song.get("mode") == "lipsync"
        return block("music", "decided", "Your song", "Lip-sync" if lipsync else "Background")
    if plan.get("narration"):
        return block("music", "decided", "Your voiceover")
    return _skipped("music")


def decided_block(plan: dict[str, Any], section: str) -> dict[str, Any]:
    """One section `decided` from a pinned guided plan. Only fields the plan carries."""
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
                summary += f" \u00b7 {round(float(duration))}s"
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
    raise ValueError(f"unknown plan block section: {section}")


def blocks_from_guided_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """All seven sections `decided` from a pinned guided execution plan (phone path:
    the render happens on the device, so the cloud decisions are the whole story)."""
    return [decided_block(plan, section) for section in SECTION_ORDER]


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
    clip_count: int | None = None,
    captions: int = 0,
    music: str | None = None,
    music_detail: str | None = None,
    look: str | None = None,
) -> list[dict[str, Any]]:
    """All seven sections `decided` for a phone recipe pinned by the narrated,
    voiceover-montage or subtitled writers (the render happens on the device, so
    these decisions are the whole story).

    The recipe supplies what it truly carries: clip count (distinct sources on the
    video track unless `clip_count` overrides it) and duration, sound-effect clips
    (the `sfx` track) and overlay clips (overlay-kind tracks other than the Talking
    cutaways, which count as clips). `title`, `captions`, `music` and `look` come from
    the caller's own decision; a falsy value means that section was not used.
    """
    cutaway_track_id = "talking-head-cutaways"
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
            summary += f" \u00b7 {round(float(duration))}s"
        by_section["clips"] = block("clips", "decided", summary)
    else:
        by_section["clips"] = _skipped("clips")
    by_section["captions"] = (
        block("captions", "decided", _count_label(captions, "caption"))
        if captions > 0
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
    return [by_section[section] for section in SECTION_ORDER]


def emit_phone_recipe_blocks(job_id: str | uuid.UUID, recipe: Any, **facts: Any) -> None:
    """Build `blocks_from_phone_recipe` and emit them. Best-effort; never raises.

    Call it from the render thread AFTER the device request is pinned and committed,
    with no Job/PlanItem/Plan lock held (same discipline as the guided path).
    """
    if not enabled():
        return
    try:
        emit_plan_blocks(job_id, blocks_from_phone_recipe(recipe, **facts))
    except Exception as exc:  # noqa: BLE001 - the feed must never fail a render
        log.warning("plan_blocks_phone_failed", job_id=str(job_id), error=str(exc)[:200])


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
