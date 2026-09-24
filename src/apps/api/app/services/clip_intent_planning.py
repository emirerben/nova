"""Inventory creator instructions independently before grounding them in footage."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalSchemaError
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.agents.clip_intent_planner import ClipIntentPlannerAgent, ClipIntentPlannerInput
from app.config import settings
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services.clip_intent_resolution import (
    IntentClip,
    IntentResolution,
    resolve_clip_intents_for_turn,
)


@dataclass(frozen=True)
class PlannedIntentResolution:
    requested_intents: list[ClipIntent]
    resolution: IntentResolution


def resolve_order_by_intent(intent: ClipIntent, clips: list[IntentClip]) -> ResolvedClipIntent:
    """Resolve a ``by_capture_time`` / ``by_route`` order deterministically (KRI-189).

    It orders EVERY clip by a fact the phone recorded, so there is nothing for a
    vision model to decide: membership is all clips, at full confidence, and the
    planner sorts by each clip's capture time.
    """
    return ResolvedClipIntent(
        **intent.model_dump(),
        status="resolved",
        assignments=[
            ClipAssignment(
                media_id=clip.media_id,
                evidence="filming order",
                confidence=1.0,
            )
            for clip in clips
        ],
    )


async def plan_and_resolve_clip_intents(
    *,
    creator_request: str,
    latest_user_message: str | None,
    candidate_intents: list[ClipIntent] | None,
    clips: list[IntentClip],
    run_context: RunContext,
    background: bool = False,
    checkpoint: Any = None,
) -> PlannedIntentResolution:
    if len(creator_request) > CREATOR_REQUEST_MAX_CHARS:
        return PlannedIntentResolution(
            [],
            IntentResolution(
                status="needs_creator",
                question=(
                    "Please restate the complete clip instructions in a shorter message "
                    "so I can preserve all of them."
                ),
            ),
        )
    # KRI-189: fact-based ordering is only understood for accounts with CLIP_FACTS.
    clip_facts_on = settings.clip_facts_for(getattr(run_context, "creator_id", None))
    # Candidates improve recall but are never the authority: even an empty
    # creative strategy must pass through the complete request inventory.
    try:
        output = await asyncio.to_thread(
            ClipIntentPlannerAgent(default_client()).run,
            ClipIntentPlannerInput(
                creator_request=creator_request,
                latest_user_message=latest_user_message,
                candidate_intents=candidate_intents,
                **({"clip_facts": True} if clip_facts_on else {}),
            ),
            ctx=run_context,
        )
    except TerminalSchemaError:
        # Invalid model output is a content-recovery problem, not a failed
        # preparation. Ask for a complete restatement and trust none of the
        # candidate intents. Refusals, transient exhaustion, and control-plane
        # failures remain terminal and are handled by their existing callers.
        return PlannedIntentResolution(
            [],
            IntentResolution(
                status="needs_creator",
                question=(
                    "I couldn't safely verify the clip-specific instructions. "
                    "Please restate which clips to use, group, order, label, or caption."
                ),
            ),
        )
    if output.question:
        return PlannedIntentResolution(
            [],
            IntentResolution(
                status="needs_creator",
                question=output.question,
            ),
        )
    intents = [
        ClipIntent.model_validate(intent.model_dump(exclude={"source_quote"}))
        for intent in output.intents
    ]
    if not clip_facts_on:
        # Defense in depth: the flag-off prompt never teaches `order_by`, so a
        # stray one is dropped rather than half-honored.
        intents = [
            intent.model_copy(update={"order_by": None}) if intent.order_by else intent
            for intent in intents
        ]
    visual_intents = [intent for intent in intents if intent.label_source == "clip"]
    order_by_intents = [intent for intent in visual_intents if intent.order_by]
    visual_intents = [intent for intent in visual_intents if not intent.order_by]
    resolved_orders = [resolve_order_by_intent(intent, clips) for intent in order_by_intents]
    # Transcript labels are fulfilled later from the pinned narration and
    # final timeline. They remain in the complete requested inventory, but
    # must never enter the vision resolver.
    if not visual_intents:
        if resolved_orders:
            return PlannedIntentResolution(intents, IntentResolution(intents=resolved_orders))
        return PlannedIntentResolution(intents, IntentResolution())
    resolution = await resolve_clip_intents_for_turn(
        intents=visual_intents,
        creator_request=creator_request,
        clips=clips,
        run_context=run_context,
        background=background,
        checkpoint=checkpoint,
    )
    if resolved_orders:
        resolution = replace(resolution, intents=[*resolution.intents, *resolved_orders])
    return PlannedIntentResolution(intents, resolution)
