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


class SlidePostProposalResult:
    """A non-persisted composition result; ``fallback_used`` is honest UI data."""

    def __init__(self, draft: SlidePostDraft, *, fallback_used: bool) -> None:
        self.draft = draft
        self.fallback_used = fallback_used


def _asset_description(asset: PlanItemAsset) -> str:
    """A short, bounded description from already-computed analysis — never
    the raw analysis dict (keeps the prompt small and private fields out)."""
    analysis = asset.analysis if isinstance(asset.analysis, dict) else {}
    text = str(analysis.get("description") or analysis.get("summary") or "").strip()
    if not text and asset.user_context:
        text = str(asset.user_context).strip()
    return text[:_MAX_DESCRIPTION_LEN]


async def propose_slide_post_draft(
    *,
    item: PlanItem,
    assets: list[PlanItemAsset],
    platform_profile: PlatformProfile,
    previous_version: int,
    current_draft: SlidePostDraft | None = None,
    instruction: str = "",
    run_context: RunContext | None = None,
) -> SlidePostProposalResult:
    """Return a new `SlidePostDraft` ordering `assets` — AI-proposed when
    possible, pool order as the fail-open fallback. Never raises."""

    previous_by_asset = (
        {ref.asset_id: ref for ref in current_draft.slides} if current_draft is not None else {}
    )
    fallback_order = [
        previous_by_asset.get(asset.id)
        or SlideRef(id=str(asset.id), asset_id=asset.id, kind=asset.kind)
        for asset in assets
    ]
    current_cover_asset_id = (
        current_draft.slides[current_draft.cover_index].asset_id
        if current_draft is not None and current_draft.slides
        else None
    )
    fallback_cover_index = next(
        (
            index
            for index, ref in enumerate(fallback_order)
            if ref.asset_id == current_cover_asset_id
        ),
        0,
    )
    fallback = SlidePostDraft(
        platform_profile=platform_profile,
        slides=fallback_order,
        cover_index=fallback_cover_index,
        caption=current_draft.caption if current_draft is not None else "",
        version=previous_version + 1,
        user_edited=False,
    )

    try:
        from app.agents._model_client import default_client  # noqa: PLC0415

        agent_input = SlidePostComposerInput(
            theme=str(item.theme or ""),
            idea=str(item.idea or ""),
            notes=(
                f"{str(item.notes or '')}\nCreator composition request: {instruction[:2000]}"
                if instruction.strip()
                else str(item.notes or "")
            ),
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
        return SlidePostProposalResult(fallback, fallback_used=True)
    except Exception as exc:  # noqa: BLE001 - a bad compose must never block assembly
        log.warning(
            "slide_post_compose_unexpected_error", plan_item_id=str(item.id), error=str(exc)[:200]
        )
        return SlidePostProposalResult(fallback, fallback_used=True)

    by_id = {str(asset.id): asset for asset in assets}
    ordered_slides = [
        SlideRef(
            # Keep client keys and manual per-slide edits across an AI proposal.
            id=(
                previous_by_asset.get(by_id[asset_id].id).id
                if by_id[asset_id].id in previous_by_asset
                else asset_id
            ),
            asset_id=by_id[asset_id].id,
            kind=by_id[asset_id].kind,
            alt=result.alt_text.get(asset_id),
            edits=(
                previous_by_asset.get(by_id[asset_id].id).edits
                if by_id[asset_id].id in previous_by_asset
                else None
            ),
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
        return SlidePostProposalResult(fallback, fallback_used=True)

    cover_index = next(
        (index for index, ref in enumerate(ordered_slides) if str(ref.asset_id) == result.cover_id),
        0,
    )
    # The established composer prompt treats notes as untrusted plan data.  The
    # bounded creator request is included there so it can affect sequencing
    # without becoming executable prompt content.
    return SlidePostProposalResult(
        SlidePostDraft(
            platform_profile=platform_profile,
            slides=ordered_slides,
            cover_index=cover_index,
            caption=result.caption,
            version=previous_version + 1,
            user_edited=False,
        ),
        fallback_used=False,
    )


async def compose_slide_post_draft(
    *,
    item: PlanItem,
    assets: list[PlanItemAsset],
    platform_profile: PlatformProfile,
    previous_version: int,
    run_context: RunContext | None = None,
) -> SlidePostDraft:
    """Backward-compatible persisted-compose helper used by the web route."""
    return (
        await propose_slide_post_draft(
            item=item,
            assets=assets,
            platform_profile=platform_profile,
            previous_version=previous_version,
            run_context=run_context,
        )
    ).draft
