"""KRI-374 x KRI-282: the song-order question and the conflict-choice question are two
independent chat questions. Each turn asks at most one; neither masks the other; the
answered pair survives to the final plan together."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.kria import planner
from app.kria.strategy_policy import CheckedStrategy
from app.models import PlanItem
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.schemas.user_song import AlignmentAlternate, SongAlignment, TakeAlignment
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentClip, IntentResolution
from app.services.song_order import build_song_order_question

GEN = 3
ITEM = uuid.uuid4()
REQUEST = "Make it chronological and group the clips by sport."
T0 = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


def _alignment() -> dict:
    takes = {
        "a": TakeAlignment(media_id="a", status="confident", delta_s=10.0, confidence=0.9),
        "b": TakeAlignment(
            media_id="b",
            status="ambiguous",
            delta_s=80.0,
            alternates=[
                AlignmentAlternate(delta_s=5.0, score=0.9),
                AlignmentAlternate(delta_s=80.0, score=0.8),
            ],
        ),
    }
    return SongAlignment(song_generation=GEN, takes=takes).model_dump(mode="json")


class _Db:
    def __init__(self) -> None:
        self.rollback = AsyncMock()
        self.item = SimpleNamespace(
            song_alignment=_alignment(), song_generation=GEN, song_analysis=None
        )

    async def get(self, model, _id, **_kw):  # noqa: ANN001, ANN202
        assert model is PlanItem
        return self.item


def _group(name: str, *ids: str) -> ResolvedClipIntent:
    intent = ClipIntent(intent_id=f"g-{name}", op="group", attribute=name)
    return ResolvedClipIntent(
        **intent.model_dump(),
        assignments=[ClipAssignment(media_id=i, evidence="x", confidence=0.9) for i in ids],
    )


# Interleaved in filming order, so "chronological" + "group by sport" really conflict.
MIXED = [_group("football", "c0", "c2", "c4"), _group("dodgeball", "c1", "c3", "c5")]


def _song_events() -> list:
    q = build_song_order_question(SongAlignment.model_validate(_alignment()), ["a", "b"])
    return [
        ("assistant", {"song_order_question": q.model_dump(mode="json")}),
        ("user", {"song_order": {"question_id": q.question_id, "ordered_media_ids": ["b", "a"]}}),
    ]


def _choice_events(option: str = "group_first") -> list:
    question = {
        "version": 1,
        "question_id": "cq1",
        "conflict": "order_vs_group",
        "options": [{"key": "group_first"}, {"key": "chronological"}],
    }
    return [
        ("assistant", {"choice_question": question}),
        ("user", {"choice_selection": {"question_id": "cq1", "option_key": option}}),
    ]


async def _turn(monkeypatch: pytest.MonkeyPatch, events: list, *, song_sync: str = "lipsync"):
    manifest = SimpleNamespace(
        media=[SimpleNamespace(media_id=m, kind="video") for m in ("a", "b")],
        narration=None,
        manifest_hash="m" * 64,
        context_hash="c" * 64,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="guided_story",
            edit_format="montage",
            audio_strategy="user_song",
            song_sync=song_sync,
            pacing="fast",
            render_program="guided",
            selected_media_ids=[],
            rationale="A chronological sports montage on the creator's song.",
        ),
        summary="A sports montage.",
    )
    resolver = AsyncMock(
        return_value=PlannedIntentResolution(
            [ClipIntent(intent_id="g", op="group", attribute="x")],
            IntentResolution(intents=MIXED),
        )
    )
    monkeypatch.setattr(settings, "user_song_montage_enabled", True)
    monkeypatch.setattr(settings, "song_alignment_turn_deadline_s", 0.0)
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(planner, "plan_and_resolve_clip_intents", resolver)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=list(events)))
    monkeypatch.setattr(
        planner,
        "check_strategy_for_runtime_v2",
        lambda _m, strategy: CheckedStrategy(strategy=strategy, notices=()),
    )
    clips = [
        IntentClip(
            media_id=f"c{i}", kind="video", analysis=None, capture_time=T0 + timedelta(minutes=i)
        )
        for i in range(6)
    ]
    result = await planner._plan_from_creator_output(
        _Db(),
        thread_id=uuid.uuid4(),
        item_id=ITEM,
        creator_id=uuid.uuid4(),
        user_message=REQUEST,
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=SimpleNamespace(), intent_clips=clips, creator_request=REQUEST
        ),
        output=SimpleNamespace(action=action),
        wants_capture_order=True,
    )
    return result, resolver


async def test_both_conditions_ask_one_question_per_turn_and_neither_masks_the_other(
    monkeypatch,
) -> None:
    # Turn 1: nothing answered. The song gate runs first: ONLY the song question.
    first, resolver = await _turn(monkeypatch, [])
    assert first.plan.song_order_question is not None
    assert first.plan.choice_question is None
    resolver.assert_not_awaited()  # the choice conflict is not even evaluated yet

    # Turn 2: song order confirmed. The choice conflict is still asked (not masked).
    second, _ = await _turn(monkeypatch, _song_events())
    assert second.plan.mode == "respond" and second.plan.turn_value == "question"
    assert second.plan.choice_question is not None
    assert second.plan.song_order_question is None
    assert not second.plan.intents  # nothing is drafted until the creator chooses

    # Turn 3: both answered. The plan carries BOTH server-owned results.
    third, _ = await _turn(monkeypatch, [*_song_events(), *_choice_events("group_first")])
    plan = third.plan
    assert plan.mode == "act"
    assert plan.song_order_question is None and plan.choice_question is None
    strategy = plan.intents[0].arguments["strategy"]
    assert strategy["ordering_choice"] == "group_first"
    assert [t["media_id"] for t in strategy["resolved_song_takes"]] == ["b", "a"]
    assert strategy["resolved_song_takes"][0]["confirmed_by_creator"] is True


async def test_a_choice_answer_turn_keeps_the_confirmed_song_order_when_the_model_flips(
    monkeypatch,
) -> None:
    """On the choice-answer turn the model may say `background`; the latest user event is
    the tapped choice (not the song answer). The confirmed order must still be applied."""
    events = [*_song_events(), *_choice_events("chronological")]
    result, _ = await _turn(monkeypatch, events, song_sync="background")
    strategy = result.plan.intents[0].arguments["strategy"]
    assert strategy["ordering_choice"] == "chronological"
    assert strategy["song_sync"] == "lipsync"
    assert [t["media_id"] for t in strategy["resolved_song_takes"]] == ["b", "a"]


def test_wire_dump_omits_both_questions_when_unset() -> None:
    dumped = planner.KriaTurnPlan(
        mode="respond", turn_value="question", response="Which?"
    ).model_dump(mode="json")
    assert "song_order_question" not in dumped and "choice_question" not in dumped
