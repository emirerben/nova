"""The generated images a phone Talking edit may place as picture cards (KRI-1253).

A tapped ``generated_overlay`` card makes one image and the creator accepts it in the native
review (``accept_generated_media`` marks the operation accepted; the image is a ready row in the
item's Visuals pool). That image is the only photo chat may place on a Talking edit, and it is
placed in two steps that share this module's rule:

* the planner lists it as ``overlays.asset_pool`` so the agent can ``add_overlay`` it, and the
  compile handler resolves the card's ``src_gcs_path`` from the same rows;
* a Save that posts a card on it pins it first (`pin_generated_overlays_for_commit`), because
  ``phone_subtitled_editor._overlays_from_sections`` accepts only a photo the job has pinned.

Any other pool photo stays refused there, as before. ``KRIA_SUGGESTION_GENERATED_OVERLAY_ENABLED``
off makes every function here a no-op, so a chat turn and a Save are what they were.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.config import settings
from app.models import GeneratedMediaOperation, PlanItemAsset
from app.services.phone_subtitled_editor import is_phone_subtitled_editor_variant, pinned_visual

MAX_POOL_ROWS = 6
NOT_IN_POOL = "generated_overlay_not_in_pool"
UNAVAILABLE = "generated_overlay_unavailable"


def enabled_for(variant: dict[str, Any] | None) -> bool:
    return bool(
        settings.kria_suggestion_generated_overlay_enabled
        and variant is not None
        and is_phone_subtitled_editor_variant(variant)
    )


def _statement(item_id: uuid.UUID, user_id: uuid.UUID):  # noqa: ANN202
    return (
        select(PlanItemAsset)
        .join(GeneratedMediaOperation, GeneratedMediaOperation.asset_id == PlanItemAsset.id)
        .where(
            GeneratedMediaOperation.plan_item_id == item_id,
            GeneratedMediaOperation.user_id == user_id,
            GeneratedMediaOperation.capability == "image",
            GeneratedMediaOperation.state == "ready",
            GeneratedMediaOperation.request_metadata["accepted"].astext == "true",
            PlanItemAsset.plan_item_id == item_id,
            PlanItemAsset.user_id == user_id,
            PlanItemAsset.status == "ready",
            PlanItemAsset.kind == "image",
            PlanItemAsset.deduplicated_to_asset_id.is_(None),
        )
        .order_by(PlanItemAsset.created_at, PlanItemAsset.id)
        .limit(MAX_POOL_ROWS)
    )


def _rows(assets: Any) -> list[dict[str, str]]:
    rows = []
    for asset in assets:
        label = " ".join(str(asset.source_filename or "").split())[:60]
        rows.append(
            {
                "id": str(asset.id),
                "kind": "image",
                "gcs_path": asset.gcs_path,
                **({"label": label} if label else {}),
            }
        )
    return rows


async def load_pool(db: AsyncSession, item_id: uuid.UUID, user_id: uuid.UUID) -> list[dict]:
    async with db.begin_nested():
        assets = (await db.execute(_statement(item_id, user_id))).scalars().all()
    return _rows(assets)


def load_pool_sync(db: Session, item_id: uuid.UUID, user_id: uuid.UUID) -> list[dict]:
    return _rows(db.execute(_statement(item_id, user_id)).scalars().all())


def paths_by_asset_id(rows: list[dict]) -> dict[str, str]:
    return {row["id"]: row["gcs_path"] for row in rows}


def _unpinned_posted_paths(job: Any, variant_id: str, payload: Any) -> set[str]:
    """The card paths a Save posts that the job has not pinned (empty unless the lane is open)."""
    if "media_overlays" not in getattr(payload, "model_fields_set", ()):
        return set()
    posted = getattr(payload, "media_overlays", None)
    if not posted:
        return set()
    from app.routes.generative_jobs import _find_variant  # noqa: PLC0415
    from app.services.phone_sources import PHONE_VISUALS_FIELD  # noqa: PLC0415

    if not enabled_for(_find_variant(job, variant_id)):
        return set()
    plan = job.assembly_plan or {}
    pinned = {row.get("gcs_path") for row in plan.get(PHONE_VISUALS_FIELD) or []}
    paths = {
        card.get("src_gcs_path") if isinstance(card, dict) else getattr(card, "src_gcs_path", None)
        for card in posted
    }
    return {path for path in paths if path} - pinned


async def pin_generated_overlays_for_commit(
    db: AsyncSession, job: Any, variant_id: str, payload: Any, *, plan_item_id: uuid.UUID
) -> None:
    """Pin the accepted generated images a Save places as cards; a no-op for any other Save."""
    from app.services.phone_speaker_background import (
        pin_pool_visual_for_background,  # noqa: PLC0415
    )

    paths = _unpinned_posted_paths(job, variant_id, payload)
    if not paths:
        return
    for row in await load_pool(db, plan_item_id, job.user_id):
        if row["gcs_path"] in paths and pinned_visual(job.assembly_plan or {}, row["id"]) is None:
            await pin_pool_visual_for_background(
                db,
                job,
                plan_item_id=plan_item_id,
                media_id=row["id"],
                kind="image",
                not_in_pool=NOT_IN_POOL,
                unavailable=UNAVAILABLE,
            )


def pin_generated_overlays_for_commit_sync(
    db: Session, job: Any, variant_id: str, payload: Any, *, plan_item_id: uuid.UUID
) -> None:
    from app.services.phone_speaker_background import (  # noqa: PLC0415
        pin_pool_visual_for_background_sync,
    )

    paths = _unpinned_posted_paths(job, variant_id, payload)
    if not paths:
        return
    for row in load_pool_sync(db, plan_item_id, job.user_id):
        if row["gcs_path"] in paths and pinned_visual(job.assembly_plan or {}, row["id"]) is None:
            pin_pool_visual_for_background_sync(
                db,
                job,
                plan_item_id=plan_item_id,
                media_id=row["id"],
                kind="image",
                not_in_pool=NOT_IN_POOL,
                unavailable=UNAVAILABLE,
            )
