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

import uuid
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
from app.services.clip_understanding import clip_record
from app.worker import celery_app

log = structlog.get_logger()
MAX_CLIPS_PER_RUN = 24


def _needs_understanding(row: object) -> bool:
    return (
        isinstance(row, dict)
        and bool(row.get("gcs_path"))
        and bool(row.get("media_id"))
        and clip_record(row.get("analysis"), kind=str(row.get("kind") or "video")).is_empty()
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
        return plan.user_id, rows[:MAX_CLIPS_PER_RUN]


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


@celery_app.task(
    bind=True,
    name="tasks.analyze_kria_clips",
    soft_time_limit=1500,
    time_limit=1560,
    max_retries=0,
)
def analyze_kria_clips(self, item_id: str) -> dict[str, Any]:  # noqa: ANN001
    """Analyse every clip of one plan item that has no stored understanding yet."""
    if not _enabled():
        return {"item_id": item_id, "status": "disabled"}
    identifier = uuid.UUID(item_id)
    try:
        found = _candidates(identifier)
    except Exception:  # noqa: BLE001 - best-effort background work
        log.warning("kria_clip_understanding_load_failed", item_id=item_id, exc_info=True)
        return {"item_id": item_id, "status": "failed"}
    if found is None:
        return {"item_id": item_id, "status": "ignored"}
    user_id, rows = found
    from app.services.creator_clip_analysis import analyze_clip_assignment  # noqa: PLC0415

    done = failed = 0
    for raw in rows:
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
            entry = _with_speech_segments(entry)
            if _store(identifier, entry):
                done += 1
        except (AiBudgetExceededError, ProviderQuotaExceededError, ProviderOutcomeUnknownError):
            # Budget/quota/unknown billing: stop, never retry-storm. A later attach re-enqueues.
            log.warning("kria_clip_understanding_stopped", item_id=item_id, exc_info=True)
            failed += 1
            break
        except Exception:  # noqa: BLE001 - one bad clip must not stop the rest
            failed += 1
            log.warning(
                "kria_clip_understanding_clip_failed",
                item_id=item_id,
                media_id=str(raw.get("media_id")),
                exc_info=True,
            )
    log.info("kria_clip_understanding_done", item_id=item_id, analyzed=done, failed=failed)
    return {"item_id": item_id, "status": "done", "analyzed": done, "failed": failed}


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
