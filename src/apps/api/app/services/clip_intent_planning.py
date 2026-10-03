"""Inventory creator instructions independently before grounding them in footage."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any

import structlog

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalSchemaError
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.agents.clip_intent_planner import (
    ClipIntentPlannerAgent,
    ClipIntentPlannerInput,
    salvage_question,
)
from app.config import settings
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services.clip_intent_resolution import (
    IntentClip,
    IntentResolution,
    resolve_clip_intents_for_turn,
)
from app.services.clip_understanding import understanding_incomplete

log = structlog.get_logger()


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
    generated_brief: str | None = None,
    background: bool = False,
    checkpoint: Any = None,
    max_vision_requeries: int | None = None,
    vision_deadline_s: float | None = None,
    require_clip_understanding: bool = False,
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
                generated_brief=generated_brief,
                candidate_intents=candidate_intents,
                **({"clip_facts": True} if clip_facts_on else {}),
            ),
            ctx=run_context,
        )
    except TerminalSchemaError as exc:
        # Invalid model output is a content-recovery problem, not a failed
        # preparation. Ask for a restatement and trust none of the candidate
        # intents. Refusals, transient exhaustion, and control-plane failures
        # remain terminal and are handled by their existing callers.
        cause = exc.__cause__
        error_class = getattr(cause, "error_class", None) or type(cause).__name__
        dropped = list(getattr(cause, "dropped", None) or [])
        # Redacted on purpose: the error CLASS and sizes only, never creator or
        # model text, so the next incident is diagnosable from logs alone.
        log.warning(
            "clip_intent_planner.terminal_schema",
            error_class=error_class,
            dropped_count=len(dropped),
            request_chars=len(creator_request),
            latest_chars=len(latest_user_message or ""),
            candidate_count=len(candidate_intents or []),
            clip_count=len(clips),
        )
        if dropped:
            question = salvage_question(0, dropped)
        else:
            question = (
                "I couldn't safely verify the clip-specific instructions. "
                "Please restate which clips to use, group, order, label, or caption."
            )
        return PlannedIntentResolution(
            [], IntentResolution(status="needs_creator", question=question)
        )
    if output.salvage_question:
        # Some instructions were valid but others could not be verified (or the
        # inventory exceeded the cap). Never act on a silent subset: ask about
        # exactly the remainder.
        return PlannedIntentResolution(
            [], IntentResolution(status="needs_creator", question=output.salvage_question)
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
    if require_clip_understanding:
        # KRI-282 L2: a clip whose background analysis has not landed has an empty
        # record, so the resolver would ask the creator about it. That is OUR gap, not
        # theirs: answer with the converging "still checking" reply instead.
        unanalysed = [c for c in clips if understanding_incomplete(c.analysis, kind=c.kind)]
        if unanalysed:
            return PlannedIntentResolution(
                intents,
                IntentResolution(
                    status="pending",
                    error_code="clip_understanding_incomplete",
                    diagnostics={
                        "reason": "clip_understanding_incomplete",
                        "unanalysed_clips": len(unanalysed),
                        "total_clips": len(clips),
                    },
                ),
            )
    resolution = await resolve_clip_intents_for_turn(
        intents=visual_intents,
        creator_request=creator_request,
        clips=clips,
        run_context=run_context,
        background=background,
        checkpoint=checkpoint,
        max_vision_requeries=max_vision_requeries,
        vision_deadline_s=vision_deadline_s,
    )
    if resolved_orders:
        resolution = replace(resolution, intents=[*resolution.intents, *resolved_orders])
    return PlannedIntentResolution(intents, resolution)
