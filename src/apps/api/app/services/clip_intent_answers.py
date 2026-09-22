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
    """Best-effort cache write for vision re-query answers on owned pool assets.

    The caller owns its outer transaction and commit. A nested transaction
    keeps a failed cache write from poisoning that transaction. Answers are
    accepted only for the exact asset generation that produced them, so an old
    provider result can never be attached to replaced media.
    """
    if not vision_answers:
        return True
    try:
        async with db.begin_nested():
            skipped = False
            for media_id, answers in vision_answers.items():
                if not answers or not media_id.startswith("asset-"):
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
        return False
    return True
