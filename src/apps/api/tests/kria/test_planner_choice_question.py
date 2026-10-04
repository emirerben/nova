"""KRI-282: the planner asks about a real chronological-vs-grouped conflict, once,
and follows the stored answer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreativeStrategy,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.kria import planner
from app.kria.strategy_policy import CheckedStrategy
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentClip, IntentResolution

T0 = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
REQUEST = "Make it chronological and group the clips by sport."


def _clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=f"c{i}", kind="video", analysis=None, capture_time=T0 + timedelta(minutes=i)
        )
        for i in range(6)
    ]


def _group(name: str, *ids: str) -> ResolvedClipIntent:
    intent = ClipIntent(intent_id=f"g-{name}", op="group", attribute=name)
    return ResolvedClipIntent(
        **intent.model_dump(),
        assignments=[ClipAssignment(media_id=i, evidence="x", confidence=0.9) for i in ids],
    )


MIXED = [_group("football", "c0", "c2", "c4"), _group("dodgeball", "c1", "c3", "c5")]
BLOCKS = [_group("football", "c0", "c1", "c2"), _group("dodgeball", "c3", "c4", "c5")]


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    resolved: list[ResolvedClipIntent],
    *,
    events=(),
    wants_capture_order: bool = True,
    flag: bool = True,
    model_choice: str | None = None,
):
    item_id = uuid.uuid4()
    manifest = ResolvedCreatorManifest(
        item_id=str(item_id),
        edit_format="montage",
        render_program="guided",
        capabilities={"dispatch_render": CapabilityAvailability(available=True)},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="guided_story",
            edit_format="montage",
            audio_strategy="licensed_music",
            pacing="fast",
            render_program="guided",
            selected_media_ids=[],
            rationale="A chronological sports montage.",
            ordering_choice=model_choice,
        ),
        summary="A sports montage.",
    )
    resolver = AsyncMock(
        return_value=PlannedIntentResolution(
            [ClipIntent(intent_id="g", op="group", attribute="x")],
            IntentResolution(intents=resolved),
        )
    )
    monkeypatch.setattr(planner.settings, "clip_intents_enabled", True)
    monkeypatch.setattr(planner.settings, "kria_choice_questions_enabled", flag)
    monkeypatch.setattr(planner, "plan_and_resolve_clip_intents", resolver)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=list(events)))
    monkeypatch.setattr(
        planner,
        "check_strategy_for_runtime_v2",
        lambda _manifest, strategy: CheckedStrategy(strategy=strategy, notices=()),
    )
    return await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=item_id,
        creator_id=uuid.uuid4(),
        user_message=REQUEST,
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=SimpleNamespace(), intent_clips=_clips(), creator_request=REQUEST
        ),
        output=SimpleNamespace(action=action),
        wants_capture_order=wants_capture_order,
    )


def _strategy(result) -> dict:
    return result.plan.intents[0].arguments["strategy"]


def _answered(option: str, qid: str = "q1") -> list:
    question = {
        "version": 1,
        "question_id": qid,
        "conflict": "order_vs_group",
        "options": [{"key": "group_first"}, {"key": "chronological"}],
    }
    return [
        ("assistant", {"choice_question": question}),
        ("user", {"choice_selection": {"question_id": qid, "option_key": option}}),
    ]


@pytest.mark.asyncio
async def test_real_conflict_asks_one_focused_question_with_options(monkeypatch) -> None:
    result = await _turn(monkeypatch, MIXED)
    plan = result.plan
    assert plan.mode == "respond" and plan.turn_value == "question"
    assert not plan.intents  # nothing is drafted until the creator chooses
    cq = plan.choice_question
    assert cq["version"] == 1 and cq["allow_free_text"] is True
    assert [o["key"] for o in cq["options"]] == ["group_first", "chronological"]
    assert [o["recommended"] for o in cq["options"]] == [True, False]
    assert "Group by sport, chronological inside each sport (recommended)" in plan.response
    assert "football and dodgeball" in plan.response


@pytest.mark.asyncio
async def test_no_question_when_clips_already_form_blocks(monkeypatch) -> None:
    result = await _turn(monkeypatch, BLOCKS)
    assert result.plan.turn_value == "action"
    assert "ordering_choice" not in _strategy(result)


@pytest.mark.asyncio
async def test_no_question_without_a_chronological_ask(monkeypatch) -> None:
    result = await _turn(monkeypatch, MIXED, wants_capture_order=False)
    assert result.plan.turn_value == "action"


@pytest.mark.parametrize("option", ["group_first", "chronological"])
@pytest.mark.asyncio
async def test_answered_conflict_is_never_asked_again_and_the_choice_is_followed(
    monkeypatch, option: str
) -> None:
    result = await _turn(monkeypatch, MIXED, events=_answered(option))
    assert result.plan.turn_value == "action"
    assert _strategy(result)["ordering_choice"] == option


@pytest.mark.asyncio
async def test_unanswered_question_is_asked_again_not_guessed(monkeypatch) -> None:
    # A question was asked but only free text came back: no selection stored.
    asked_only = _answered("group_first")[:1] + [("user", {})]
    result = await _turn(monkeypatch, MIXED, events=asked_only)
    assert result.plan.turn_value == "question" and result.plan.choice_question


@pytest.mark.asyncio
async def test_flag_off_is_byte_identical_to_before(monkeypatch) -> None:
    result = await _turn(monkeypatch, MIXED, flag=False, events=_answered("group_first"))
    assert result.plan.turn_value == "action"
    assert "ordering_choice" not in _strategy(result)
    assert not result.plan.choice_question


@pytest.mark.asyncio
async def test_a_model_authored_choice_is_never_trusted(monkeypatch) -> None:
    # Even if the model wrote ordering_choice, only the stored answer decides.
    result = await _turn(monkeypatch, BLOCKS, model_choice="group_first")
    assert "ordering_choice" not in _strategy(result)
    answered = await _turn(
        monkeypatch, BLOCKS, model_choice="group_first", events=_answered("chronological")
    )
    assert _strategy(answered)["ordering_choice"] == "chronological"
