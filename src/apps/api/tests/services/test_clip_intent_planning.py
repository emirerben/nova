"""Complete creator instruction inventory precedes footage grounding."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents._runtime import (
    ModelInvocation,
    ProviderQuotaExceededError,
    RunContext,
    TerminalError,
    TerminalSchemaError,
)
from app.agents.clip_intent_planner import ClipIntentPlannerOutput, PlannedClipIntent
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_planning as service
from app.services.clip_intent_resolution import IntentResolution
from tests.agents.conftest import MockModelClient, max_tokens_response


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
async def test_planner_schema_failure_requests_clarification_without_partial_candidates(
    monkeypatch,
):
    client = MockModelClient()
    invalid_response = {"intents": "not-a-list", "question": None}
    client.queue("gemini-2.5-flash", invalid_response, invalid_response)
    monkeypatch.setattr(service, "default_client", lambda: client)
    resolver = AsyncMock(return_value=IntentResolution())
    monkeypatch.setattr(service, "resolve_clip_intents_for_turn", resolver)

    result = await service.plan_and_resolve_clip_intents(
        creator_request="Group the Paris clips and label each location.",
        latest_user_message=None,
        candidate_intents=[ClipIntent(intent_id="candidate", op="label", attribute="city")],
        clips=[],
        run_context=RunContext(),
        background=True,
    )

    assert result.requested_intents == []
    assert result.resolution.needs_creator
    assert "restate" in (result.resolution.question or "").lower()
    assert len(client.invocations) == 2
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_planner_output_truncation_remains_a_retryable_preparation_failure(monkeypatch):
    client = MockModelClient()
    client.queue(
        "gemini-2.5-flash",
        ModelInvocation(
            raw_text="",
            raw_response=max_tokens_response(),
            tokens_in=10,
            tokens_out=0,
        ),
    )
    monkeypatch.setattr(service, "default_client", lambda: client)
    resolver = AsyncMock(return_value=IntentResolution())
    monkeypatch.setattr(service, "resolve_clip_intents_for_turn", resolver)

    with pytest.raises(TerminalError) as caught:
        await service.plan_and_resolve_clip_intents(
            creator_request="Group the Paris clips and label each location.",
            latest_user_message=None,
            candidate_intents=None,
            clips=[],
            run_context=RunContext(),
            background=True,
        )

    assert not isinstance(caught.value, TerminalSchemaError)
    assert len(client.invocations) == 1
    resolver.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        TerminalError("nova.plan.clip_intent_planner: refusal"),
        TerminalError("nova.plan.clip_intent_planner: exhausted 1 model after retries"),
        ProviderQuotaExceededError(provider="gemini", reason="monthly_cap"),
    ],
)
async def test_non_schema_terminal_failures_still_propagate(monkeypatch, failure):
    agent, resolver = wire(monkeypatch, ClipIntentPlannerOutput())
    agent.run.side_effect = failure

    with pytest.raises(type(failure)) as caught:
        await service.plan_and_resolve_clip_intents(
            creator_request="Group the Paris clips and label each location.",
            latest_user_message=None,
            candidate_intents=None,
            clips=[],
            run_context=RunContext(),
            background=True,
        )

    assert caught.value is failure
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
