"""Complete creator instruction inventory precedes footage grounding."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import ClipIntentPlannerOutput, PlannedClipIntent
from app.services import clip_intent_planning as service
from app.services.clip_intent_resolution import IntentResolution


def wire(monkeypatch, output):
    agent = MagicMock()
    agent.run.return_value = output
    monkeypatch.setattr(service, "default_client", MagicMock())
    monkeypatch.setattr(service, "ClipIntentPlannerAgent", MagicMock(return_value=agent))
    resolver = AsyncMock(return_value=IntentResolution())
    monkeypatch.setattr(service, "resolve_clip_intents_for_turn", resolver)
    return agent, resolver


@pytest.mark.asyncio
async def test_missing_creative_intents_still_extracts_every_travel_operation(monkeypatch):
    request = "Group the Paris clips, label the city and activity on each clip."
    intents = [
        PlannedClipIntent(intent_id=key, op=op, attribute=attribute, source_quote=request)
        for key, op, attribute in [
            ("city-group", "group", "Paris clips"),
            ("city-label", "label", "city"),
            ("activity-label", "label", "activity"),
        ]
    ]
    agent, resolver = wire(monkeypatch, ClipIntentPlannerOutput(intents=intents))
    ctx = RunContext()
    result = await service.plan_and_resolve_clip_intents(
        creator_request=request,
        latest_user_message="Make it 60 seconds",
        candidate_intents=None,
        clips=[],
        run_context=ctx,
    )
    assert len(result.requested_intents) == 3
    assert resolver.await_args.kwargs["intents"] == result.requested_intents
    assert resolver.await_args.kwargs["run_context"] is ctx
    assert agent.run.call_args.args[0].latest_user_message == "Make it 60 seconds"
    assert all("source_quote" not in intent.model_dump() for intent in result.requested_intents)


@pytest.mark.asyncio
async def test_ambiguous_instruction_never_resolves_partial_subset(monkeypatch):
    _, resolver = wire(
        monkeypatch, ClipIntentPlannerOutput(question="Which instruction should I keep?")
    )
    result = await service.plan_and_resolve_clip_intents(
        creator_request="Use those labels",
        latest_user_message=None,
        candidate_intents=None,
        clips=[],
        run_context=RunContext(),
    )
    assert result.resolution.needs_creator
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_overlong_history_never_silently_truncates_requirements(monkeypatch):
    agent, resolver = wire(monkeypatch, ClipIntentPlannerOutput())
    result = await service.plan_and_resolve_clip_intents(
        creator_request="x" * 12001,
        latest_user_message=None,
        candidate_intents=None,
        clips=[],
        run_context=RunContext(),
    )
    assert result.resolution.needs_creator
    agent.run.assert_not_called()
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_provider_failure_never_falls_back_to_partial_candidates(monkeypatch):
    agent, resolver = wire(monkeypatch, ClipIntentPlannerOutput())
    agent.run.side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        await service.plan_and_resolve_clip_intents(
            creator_request="Label the dish",
            latest_user_message=None,
            candidate_intents=None,
            clips=[],
            run_context=RunContext(),
        )
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_mixed_sources_preserve_transcript_intents_but_resolve_only_clip_intents(monkeypatch):
    request = "Label the sport on each clip and show the score I say."
    intents = [
        PlannedClipIntent(
            intent_id="sport",
            op="label",
            attribute="the sport on each clip",
            source_quote="Label the sport on each clip",
        ),
        PlannedClipIntent(
            intent_id="score",
            op="label",
            attribute="the score I say",
            label_source="transcript",
            transcript_kind="score",
            source_quote="show the score I say",
        ),
    ]
    _, resolver = wire(monkeypatch, ClipIntentPlannerOutput(intents=intents))

    result = await service.plan_and_resolve_clip_intents(
        creator_request=request,
        latest_user_message=None,
        candidate_intents=None,
        clips=[],
        run_context=RunContext(),
    )

    assert [intent.model_dump(mode="json") for intent in result.requested_intents] == [
        intent.model_dump(mode="json", exclude={"source_quote"}) for intent in intents
    ]
    assert resolver.await_args.kwargs["intents"] == [result.requested_intents[0]]


@pytest.mark.asyncio
async def test_transcript_only_inventory_never_calls_visual_resolver(monkeypatch):
    request = "Show the score I say."
    intents = [
        PlannedClipIntent(
            intent_id="score",
            op="label",
            attribute="the score I say",
            label_source="transcript",
            transcript_kind="score",
            source_quote=request,
        )
    ]
    _, resolver = wire(monkeypatch, ClipIntentPlannerOutput(intents=intents))

    result = await service.plan_and_resolve_clip_intents(
        creator_request=request,
        latest_user_message=None,
        candidate_intents=None,
        clips=[],
        run_context=RunContext(),
    )

    assert result.requested_intents[0].transcript_kind == "score"
    assert not result.resolution.needs_creator
    resolver.assert_not_called()
