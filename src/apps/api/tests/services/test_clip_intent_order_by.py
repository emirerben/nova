"""KRI-189: `by_capture_time` / `by_route` order intents.

The planner extracts them (only when CLIP_FACTS is on for the creator), the service
resolves them deterministically over every clip (no vision model), and nothing
about the flag-off path changes.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import (
    ClipIntentPlannerAgent,
    ClipIntentPlannerInput,
    ClipIntentPlannerOutput,
    PlannedClipIntent,
)
from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent
from app.services import clip_intent_planning as service
from app.services.clip_intent_resolution import IntentClip, IntentResolution

_REQUEST = "Cut it in the order I filmed them and label each landmark."


def _agent() -> ClipIntentPlannerAgent:
    return ClipIntentPlannerAgent(None)  # type: ignore[arg-type]


def _raw(**order_extra: object) -> str:
    return json.dumps(
        {
            "intents": [
                {
                    "intent_id": "chrono",
                    "op": "order",
                    "attribute": "the order I filmed them",
                    "source_quote": "in the order I filmed them",
                    **order_extra,
                },
                {
                    "intent_id": "landmarks",
                    "op": "label",
                    "attribute": "each landmark",
                    "source_quote": "label each landmark",
                },
            ],
            "question": None,
        }
    )


# ── schema ───────────────────────────────────────────────────────────────────


def test_order_by_round_trips_and_is_omitted_when_unset() -> None:
    intent = ClipIntent(intent_id="a", op="order", attribute="x", order_by="capture_time")
    assert intent.model_dump(mode="json")["order_by"] == "capture_time"
    assert "order_by" not in ClipIntent(intent_id="a", op="order", attribute="x").model_dump()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"op": "label", "order_by": "capture_time"},
        {"op": "order", "order_by": "capture_time", "position": "first"},
        {"op": "order", "order_by": "alphabetical"},
    ],
)
def test_order_by_is_only_valid_for_a_plain_order_intent(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        ClipIntent(intent_id="a", attribute="x", **kwargs)


# ── planner agent ────────────────────────────────────────────────────────────


def test_parse_accepts_order_by_when_clip_facts_are_on() -> None:
    agent_input = ClipIntentPlannerInput(creator_request=_REQUEST, clip_facts=True)
    out = _agent().parse(_raw(order_by="capture_time"), agent_input)
    assert [(i.op, i.order_by) for i in out.intents] == [
        ("order", "capture_time"),
        ("label", None),
    ]


def test_parse_drops_a_stray_order_by_intent_when_clip_facts_are_off() -> None:
    """Flag off behaves as if the field did not exist: no SchemaError, no retry."""
    agent_input = ClipIntentPlannerInput(creator_request=_REQUEST)
    out = _agent().parse(_raw(order_by="capture_time"), agent_input)
    assert [(i.op, i.order_by) for i in out.intents] == [("label", None)]


def test_prompt_teaches_order_by_only_when_clip_facts_are_on() -> None:
    on = _agent().render_prompt(ClipIntentPlannerInput(creator_request=_REQUEST, clip_facts=True))
    off = _agent().render_prompt(ClipIntentPlannerInput(creator_request=_REQUEST))
    assert "order_by" in on and '"capture_time"' in on
    assert "order_by" not in off
    assert "$order_by_note" not in on and "$order_by_note" not in off
    # Flag off: the `order` bullet is exactly what it was before the slot existed.
    assert "- order: membership moved first or last\n- include:" in off


def test_input_dump_is_unchanged_when_flag_off() -> None:
    assert "clip_facts" not in ClipIntentPlannerInput(creator_request=_REQUEST).model_dump()


# ── service: deterministic resolution ────────────────────────────────────────


def _wire(monkeypatch, intents: list[PlannedClipIntent]):
    agent = MagicMock()
    agent.run.return_value = ClipIntentPlannerOutput(intents=intents)
    monkeypatch.setattr(service, "default_client", MagicMock())
    monkeypatch.setattr(service, "ClipIntentPlannerAgent", MagicMock(return_value=agent))
    label_resolution = IntentResolution(
        intents=[ResolvedClipIntent(intent_id="landmarks", op="label", attribute="each landmark")]
    )
    resolver = AsyncMock(return_value=label_resolution)
    monkeypatch.setattr(service, "resolve_clip_intents_for_turn", resolver)
    return agent, resolver


def _clips() -> list[IntentClip]:
    return [IntentClip(media_id=f"clip-{n}", kind="video", analysis=None) for n in (1, 2, 3)]


def _planned() -> list[PlannedClipIntent]:
    return [
        PlannedClipIntent(
            intent_id="chrono",
            op="order",
            attribute="the order I filmed them",
            order_by="capture_time",
            source_quote="in the order I filmed them",
        ),
        PlannedClipIntent(
            intent_id="landmarks",
            op="label",
            attribute="each landmark",
            source_quote="label each landmark",
        ),
    ]


@pytest.mark.asyncio
async def test_order_by_is_resolved_over_every_clip_without_the_vision_resolver(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service.settings, "clip_facts_enabled", True)
    agent, resolver = _wire(monkeypatch, _planned())
    result = await service.plan_and_resolve_clip_intents(
        creator_request=_REQUEST,
        latest_user_message=None,
        candidate_intents=None,
        clips=_clips(),
        run_context=RunContext(creator_id=str(uuid.uuid4())),
    )

    assert agent.run.call_args.args[0].clip_facts is True
    # The vision resolver only ever saw the label intent.
    assert [i.intent_id for i in resolver.await_args.kwargs["intents"]] == ["landmarks"]
    by_id = {i.intent_id: i for i in result.resolution.intents}
    chrono = by_id["chrono"]
    assert (chrono.status, chrono.op, chrono.order_by) == ("resolved", "order", "capture_time")
    assert [a.media_id for a in chrono.assignments] == ["clip-1", "clip-2", "clip-3"]
    assert set(by_id) == {"chrono", "landmarks"}
    assert len(result.requested_intents) == 2


@pytest.mark.asyncio
async def test_only_order_by_never_calls_the_resolver(monkeypatch) -> None:
    monkeypatch.setattr(service.settings, "clip_facts_enabled", True)
    _agent_mock, resolver = _wire(monkeypatch, _planned()[:1])
    result = await service.plan_and_resolve_clip_intents(
        creator_request=_REQUEST,
        latest_user_message=None,
        candidate_intents=None,
        clips=_clips(),
        run_context=RunContext(creator_id=str(uuid.uuid4())),
    )
    resolver.assert_not_called()
    assert [i.intent_id for i in result.resolution.intents] == ["chrono"]
    assert not result.resolution.needs_creator


@pytest.mark.asyncio
async def test_flag_off_strips_a_stray_order_by_and_never_teaches_it(monkeypatch) -> None:
    monkeypatch.setattr(service.settings, "clip_facts_enabled", False)
    monkeypatch.setattr(service.settings, "clip_facts_user_ids", [])
    agent, resolver = _wire(monkeypatch, _planned())
    result = await service.plan_and_resolve_clip_intents(
        creator_request=_REQUEST,
        latest_user_message=None,
        candidate_intents=None,
        clips=_clips(),
        run_context=RunContext(creator_id=str(uuid.uuid4())),
    )
    assert "clip_facts" not in agent.run.call_args.args[0].model_dump()
    assert all(i.order_by is None for i in result.requested_intents)
    # Both intents (the stripped order and the label) go through the resolver as before.
    assert {i.intent_id for i in resolver.await_args.kwargs["intents"]} == {"chrono", "landmarks"}


@pytest.mark.asyncio
async def test_allowlisted_creator_gets_order_by_while_the_global_flag_is_off(monkeypatch) -> None:
    creator = uuid.uuid4()
    monkeypatch.setattr(service.settings, "clip_facts_enabled", False)
    monkeypatch.setattr(service.settings, "clip_facts_user_ids", [creator])
    agent, _resolver = _wire(monkeypatch, _planned())
    await service.plan_and_resolve_clip_intents(
        creator_request=_REQUEST,
        latest_user_message=None,
        candidate_intents=None,
        clips=_clips(),
        run_context=RunContext(creator_id=str(creator)),
    )
    assert agent.run.call_args.args[0].clip_facts is True
