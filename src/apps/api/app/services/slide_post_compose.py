"""Compose a slide-post draft (ordering, cover, caption) from an item's ready
`PlanItemAsset` pool.

Deliberately best-effort: the agent proposes, but a refusal/timeout/quota
failure must never block a creator from assembling their own post — falls
back to pool order, first slide as cover, and no caption rather than
raising. Never runs new media analysis; reads only each asset's
already-computed `analysis` payload (plans/024).
"""

from __future__ import annotations

import asyncio

import structlog

from app.agents._runtime import RunContext, TerminalError
from app.agents.slide_post_composer import (
    SlidePostComposerAgent,
    SlidePostComposerInput,
    SlidePostMediaItem,
)
from app.models import PlanItem, PlanItemAsset
from app.pipeline.slide_post.profiles import PlatformProfile
from app.schemas.slide_post import SlidePostDraft, SlideRef

log = structlog.get_logger()

_MAX_DESCRIPTION_LEN = 300


def _asset_description(asset: PlanItemAsset) -> str:
    """A short, bounded description from already-computed analysis — never
    the raw analysis dict (keeps the prompt small and private fields out)."""
    analysis = asset.analysis if isinstance(asset.analysis, dict) else {}
    text = str(analysis.get("description") or analysis.get("summary") or "").strip()
    if not text and asset.user_context:
        text = str(asset.user_context).strip()
    return text[:_MAX_DESCRIPTION_LEN]


async def compose_slide_post_draft(
    *,
    item: PlanItem,
    assets: list[PlanItemAsset],
    platform_profile: PlatformProfile,
    previous_version: int,
    run_context: RunContext | None = None,
) -> SlidePostDraft:
    """Return a new `SlidePostDraft` ordering `assets` — AI-proposed when
    possible, pool order as the fail-open fallback. Never raises."""

    fallback_order = [
        SlideRef(id=str(asset.id), asset_id=asset.id, kind=asset.kind) for asset in assets
    ]
    fallback = SlidePostDraft(
        platform_profile=platform_profile,
        slides=fallback_order,
        cover_index=0,
        caption="",
        version=previous_version + 1,
        user_edited=False,
    )

    try:
        from app.agents._model_client import default_client  # noqa: PLC0415

        agent_input = SlidePostComposerInput(
            theme=str(item.theme or ""),
            idea=str(item.idea or ""),
            notes=str(item.notes or ""),
            platform_profile=platform_profile,
            media=[
                SlidePostMediaItem(
                    id=str(asset.id), kind=asset.kind, description=_asset_description(asset)
                )
                for asset in assets
            ],
        )
        result = await asyncio.to_thread(
            SlidePostComposerAgent(default_client()).run,
            agent_input,
            ctx=run_context,
        )
    except TerminalError as exc:
        log.warning("slide_post_compose_failed", plan_item_id=str(item.id), error=str(exc)[:200])
        return fallback
    except Exception as exc:  # noqa: BLE001 - a bad compose must never block assembly
        log.warning(
            "slide_post_compose_unexpected_error", plan_item_id=str(item.id), error=str(exc)[:200]
        )
        return fallback

    by_id = {str(asset.id): asset for asset in assets}
    ordered_slides = [
        SlideRef(
            id=asset_id,
            asset_id=by_id[asset_id].id,
            kind=by_id[asset_id].kind,
            alt=result.alt_text.get(asset_id),
        )
        for asset_id in result.order
        if asset_id in by_id
    ]
    if len(ordered_slides) != len(assets):
        # The agent's parse() already enforces exact id coverage, but this is
        # the second, independent check at the boundary where the agent's
        # output crosses into the render-affecting draft — never trust a
        # single validation layer for "every id, exactly once".
        log.warning("slide_post_compose_incomplete_order", plan_item_id=str(item.id))
        return fallback

    cover_index = next((i for i, ref in enumerate(ordered_slides) if ref.id == result.cover_id), 0)
    return SlidePostDraft(
        platform_profile=platform_profile,
        slides=ordered_slides,
        cover_index=cover_index,
        caption=result.caption,
        version=previous_version + 1,
        user_edited=False,
    )
