"""Persist generation-fenced clip-intent vision answer caches."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PlanItem, PlanItemAsset
from app.services.clip_intent_resolution import ANSWERS_KEY

log = structlog.get_logger()


class ClipIntentAnswerPersistenceError(RuntimeError):
    """A required answer cache could not be safely committed."""


async def persist_clip_intent_vision_answers(
    db: AsyncSession,
    item: PlanItem,
    vision_answers: dict[str, dict[str, dict[str, Any]]],
    *,
    creator_id: uuid.UUID | None = None,
    strict: bool = False,
) -> bool:
    """Best-effort cache write for vision re-query answers on owned clips.

    The caller owns its outer transaction and commit. A nested transaction
    keeps a failed cache write from poisoning that transaction. Answers are
    accepted only for the exact asset generation that produced them, so an old
    provider result can never be attached to replaced media.

    ``strict`` raises when a pool asset's answers cannot be written (missing,
    foreign, or replaced). Raw ``clip_assignments`` media (every iPhone clip)
    are cached on their assignment instead (KRI-433), best effort in both modes:
    their cache bookkeeping never fails a turn (KRI-291).
    """
    if not vision_answers:
        return True
    assignment_answers = {
        media_id: answers
        for media_id, answers in vision_answers.items()
        if not media_id.startswith("asset-") and answers
    }
    if assignment_answers:
        # Before the asset rows: the canonical row-lock order takes PlanItem first.
        await _persist_clip_assignment_answers(db, item, assignment_answers)
    ok = True
    try:
        async with db.begin_nested():
            skipped = False
            for media_id, answers in vision_answers.items():
                if not media_id.startswith("asset-"):
                    continue
                if not answers:
                    skipped = True
                    continue
                try:
                    asset_uuid = uuid.UUID(media_id.removeprefix("asset-"))
                except ValueError:
                    skipped = True
                    continue
                asset = await db.get(
                    PlanItemAsset, asset_uuid, with_for_update=True, populate_existing=True
                )
                if (
                    asset is None
                    or asset.plan_item_id != item.id
                    or (creator_id is not None and getattr(asset, "user_id", None) != creator_id)
                ):
                    skipped = True
                    continue
                generation = str(getattr(asset, "gcs_generation", None) or "")
                fresh = {
                    key: value
                    for key, value in answers.items()
                    if isinstance(value, dict) and str(value.get("generation") or "") == generation
                }
                if len(fresh) != len(answers):
                    skipped = True
                    if strict:
                        raise ClipIntentAnswerPersistenceError("answer_cache_target_changed")
                if not fresh:
                    continue
                analysis = dict(asset.analysis) if isinstance(asset.analysis, dict) else {}
                existing = dict(analysis.get(ANSWERS_KEY) or {})
                existing.update(fresh)
                analysis[ANSWERS_KEY] = existing
                asset.analysis = analysis
            if strict and skipped:
                raise ClipIntentAnswerPersistenceError("answer_cache_target_changed")
            await db.flush()
    except Exception as exc:  # noqa: BLE001
        log.warning("clip_intents.vision_answer_persist_failed", error=str(exc)[:300])
        if strict:
            raise
        ok = False
    return ok


class _FootageIdentityChanged(RuntimeError):
    """An analysis-only write would have changed footage identity; undo it."""


async def _persist_clip_assignment_answers(
    db: AsyncSession,
    item: PlanItem,
    vision_answers: dict[str, dict[str, dict[str, Any]]],
) -> None:
    """Cache answers on the clip's own ``clip_assignments`` analysis (KRI-433).

    Every iPhone clip is a raw assignment, not a pool asset. Without this cache a
    turn that ran out of vision budget re-asked every question on the next turn, so
    "go ahead" could stall on the same cap again. The resolver already reads
    ``analysis["answers"]`` from the assignment (``_cached_answer``); this is the
    missing writer. Fenced like ``kria_clip_understanding._store``: the item row is
    locked, an answer lands only on the assignment with the same media id and
    storage generation that produced it (a clip without a recorded generation is
    never cached), and the write goes through the media facade and is undone if it
    would change footage identity. Any failure only loses the cache.
    """
    from app.services.plan_item_media import (  # noqa: PLC0415
        current_detector_policy,
        mutate_plan_item_media,
    )
    from app.services.speech_cleanup_preflight import (  # noqa: PLC0415
        mutation_current_analysis_async,
    )

    try:
        async with db.begin_nested():
            locked = await db.get(PlanItem, item.id, with_for_update=True, populate_existing=True)
            if locked is None:
                return
            rows: list[Any] = []
            cached_clips = 0
            for row in locked.clip_assignments or []:
                answers = (
                    vision_answers.get(str(row.get("media_id") or ""))
                    if isinstance(row, dict)
                    else None
                )
                generation = (
                    str(row.get("storage_generation") or row.get("generation") or "")
                    if answers
                    else ""
                )
                fresh = {
                    key: value
                    for key, value in (answers or {}).items()
                    if generation
                    and isinstance(value, dict)
                    and str(value.get("generation") or "") == generation
                }
                if not fresh:
                    rows.append(row)
                    continue
                analysis = dict(row["analysis"]) if isinstance(row.get("analysis"), dict) else {}
                existing = analysis.get(ANSWERS_KEY)
                analysis[ANSWERS_KEY] = {
                    **(existing if isinstance(existing, dict) else {}),
                    **fresh,
                }
                rows.append({**row, "analysis": analysis})
                cached_clips += 1
            if not cached_clips:
                return
            result = mutate_plan_item_media(
                locked,
                detector_policy=current_detector_policy(),
                clip_assignments=rows,
                current_analysis=await mutation_current_analysis_async(
                    db, locked.id, for_update=True
                ),
            )
            if result.source_changed:
                raise _FootageIdentityChanged("answer cache would change footage identity")
            await db.flush()
        log.info("clip_intents.clip_assignment_answers_cached", clips=cached_clips)
    except Exception as exc:  # noqa: BLE001 - a lost cache only costs a re-ask next turn
        log.warning(
            "clip_intents.clip_assignment_answer_persist_failed",
            error_type=type(exc).__name__,
        )
