"""Inventory creator instructions independently before grounding them in footage."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.agents._model_client import default_client
from app.agents._runtime import RunContext
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.agents.clip_intent_planner import ClipIntentPlannerAgent, ClipIntentPlannerInput
from app.schemas.clip_intents import ClipIntent
from app.services.clip_intent_resolution import (
    IntentClip,
    IntentResolution,
    resolve_clip_intents_for_turn,
)


@dataclass(frozen=True)
class PlannedIntentResolution:
    requested_intents: list[ClipIntent]
    resolution: IntentResolution


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
    # Candidates improve recall but are never the authority: even an empty
    # creative strategy must pass through the complete request inventory.
    output = await asyncio.to_thread(
        ClipIntentPlannerAgent(default_client()).run,
        ClipIntentPlannerInput(
            creator_request=creator_request,
            latest_user_message=latest_user_message,
            candidate_intents=candidate_intents,
        ),
        ctx=run_context,
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
    visual_intents = [intent for intent in intents if intent.label_source == "clip"]
    # Transcript labels are fulfilled later from the pinned narration and
    # final timeline. They remain in the complete requested inventory, but
    # must never enter the vision resolver.
    if not visual_intents:
        return PlannedIntentResolution(intents, IntentResolution())
    resolution = await resolve_clip_intents_for_turn(
        intents=visual_intents,
        creator_request=creator_request,
        clips=clips,
        run_context=run_context,
        background=background,
        checkpoint=checkpoint,
    )
    return PlannedIntentResolution(intents, resolution)
