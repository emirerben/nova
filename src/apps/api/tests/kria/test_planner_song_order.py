"""KRI-374 lane E: planner gate for lip-sync song montages (pending / question / proceed)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.kria import planner
from app.kria.planner import _plan_from_creator_output, adapt_creator_action
from app.kria.strategy_policy import CheckedStrategy
from app.models import PlanItem
from app.schemas.user_song import (
    AlignmentAlternate,
    SongAlignment,
    SongOrderQuestion,
    TakeAlignment,
)
from app.services.song_order import build_song_order_question

THREAD = uuid.uuid4()
ITEM = uuid.uuid4()
CREATOR = uuid.uuid4()
GEN = 4


def _media(*ids: str) -> list:
    return [SimpleNamespace(media_id=i, kind="video") for i in ids]


def _manifest(*ids: str) -> SimpleNamespace:
    return SimpleNamespace(media=_media(*ids), manifest_hash="m" * 64, context_hash="c" * 64)


def _strategy(**kw) -> SimpleNamespace:
    base = {"audio_strategy": "user_song", "song_sync": "lipsync", "selected_media_ids": []}
    return SimpleNamespace(**{**base, **kw})


def _conf(mid: str, d: float) -> TakeAlignment:
    return TakeAlignment(media_id=mid, status="confident", delta_s=d, confidence=0.9)


def _amb(mid: str, d: float, alts: list[tuple[float, float]]) -> TakeAlignment:
    return TakeAlignment(
        media_id=mid,
        status="ambiguous",
        delta_s=d,
        alternates=[AlignmentAlternate(delta_s=a, score=s) for a, s in alts],
    )


def _alignment(*takes: TakeAlignment) -> dict:
    return SongAlignment(song_generation=GEN, takes={t.media_id: t for t in takes}).model_dump(
        mode="json"
    )


class _Db:
    """get() returns the (mutable) item; execute()/rollback() are no-ops."""

    def __init__(self, item) -> None:  # noqa: ANN001
        self.item = item
        self.gets = 0
        self.rollback = AsyncMock()

    async def get(self, model, _id, **_kw):  # noqa: ANN001, ANN202
        assert model is PlanItem
        self.gets += 1
        return self.item


def _item(alignment: dict | None, analysis: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(song_alignment=alignment, song_generation=GEN, song_analysis=analysis)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "user_song_montage_enabled", True)
    monkeypatch.setattr(settings, "song_alignment_turn_deadline_s", 0.0)
    monkeypatch.setattr(planner, "_SONG_ALIGNMENT_POLL_S", 0.01)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))


async def _gate(db, manifest, strategy):  # noqa: ANN001, ANN202
    return await planner._song_order_gate(
        db, thread_id=THREAD, item_id=ITEM, manifest=manifest, strategy=strategy
    )


async def test_alignment_missing_returns_the_song_pending_reply() -> None:
    db = _Db(_item(None))
    result = await _gate(db, _manifest("a", "b"), _strategy())
    assert result.plan is not None and result.plan.turn_value == "recovery"
    # Its own wording -- not the clip-picker "checking some of your clips" copy.
    assert result.plan.response == planner._SONG_PENDING_REPLY
    assert "song" in result.plan.response
    assert result.plan.response != planner._CLIP_INTENT_PENDING_REPLY
    assert result.plan.diagnostics["reason"] == "song_alignment_pending"
    assert result.plan.song_order_question is None


async def test_stale_or_incomplete_alignment_is_pending() -> None:
    stale = {**_alignment(_conf("a", 1.0), _conf("b", 2.0)), "song_generation": GEN - 1}
    assert (await _gate(_Db(_item(stale)), _manifest("a", "b"), _strategy())).plan is not None
    incomplete = _alignment(_conf("a", 1.0))
    assert (await _gate(_Db(_item(incomplete)), _manifest("a", "b"), _strategy())).plan is not None


async def test_bounded_wait_picks_up_alignment_that_lands_mid_wait(monkeypatch) -> None:
    monkeypatch.setattr(settings, "song_alignment_turn_deadline_s", 5.0)
    item = _item(None)
    db = _Db(item)

    async def _sleep(_s: float) -> None:
        item.song_alignment = _alignment(_conf("a", 1.0), _conf("b", 9.0))

    monkeypatch.setattr(planner.asyncio, "sleep", _sleep)
    result = await _gate(db, _manifest("a", "b"), _strategy())
    assert result.plan is None and result.resolved_takes is None  # all confident
    assert db.gets == 2


async def test_wait_is_bounded_by_the_deadline(monkeypatch) -> None:
    monkeypatch.setattr(settings, "song_alignment_turn_deadline_s", 0.05)
    db = _Db(_item(None))
    result = await _gate(db, _manifest("a"), _strategy())
    assert result.plan is not None and result.plan.turn_value == "recovery"
    assert db.gets >= 2  # it really polled before giving up


async def test_all_confident_asks_nothing() -> None:
    db = _Db(_item(_alignment(_conf("a", 1.0), _conf("b", 9.0))))
    result = await _gate(db, _manifest("a", "b"), _strategy())
    assert result.plan is None and result.resolved_takes is None


async def test_uncertain_take_without_answer_returns_a_question() -> None:
    db = _Db(_item(_alignment(_conf("a", 10.0), _amb("b", 30.0, [(30.0, 0.9), (80.0, 0.8)]))))
    result = await _gate(db, _manifest("a", "b"), _strategy())
    plan = result.plan
    assert plan is not None and plan.mode == "respond" and plan.turn_value == "question"
    q = plan.song_order_question
    assert isinstance(q, SongOrderQuestion)
    assert q.proposed_order == ["a", "b"]
    assert [i.status for i in q.items] == ["confident", "ambiguous"]
    dumped = plan.model_dump(mode="json")
    assert dumped["song_order_question"]["items"][1]["alternates"][1]["delta_s"] == 80.0


async def test_answer_resolves_uncertain_takes_onto_the_strategy_payload(monkeypatch) -> None:
    alignment = _alignment(
        _conf("a", 10.0), _amb("b", 30.0, [(30.0, 0.9), (80.0, 0.95)]), _conf("c", 60.0)
    )
    q = build_song_order_question(SongAlignment.model_validate(alignment), ["a", "b", "c"])
    events = [
        ("assistant", {"song_order_question": q.model_dump(mode="json")}),
        (
            "user",
            {"song_order": {"question_id": q.question_id, "ordered_media_ids": ["a", "c", "b"]}},
        ),
    ]
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events))
    result = await _gate(_Db(_item(alignment)), _manifest("a", "b", "c"), _strategy())
    assert result.plan is None
    assert result.resolved_takes == [
        {"media_id": "a", "delta_s": 10.0, "status": "confident", "confirmed_by_creator": False},
        {"media_id": "c", "delta_s": 60.0, "status": "confident", "confirmed_by_creator": False},
        {"media_id": "b", "delta_s": 80.0, "status": "confident", "confirmed_by_creator": True},
    ]


async def test_answer_for_a_previous_song_generation_is_asked_again(monkeypatch) -> None:
    """The song was replaced after the creator answered: the old order is not reused."""
    alignment = _alignment(_conf("a", 10.0), _amb("b", 30.0, [(30.0, 0.9), (80.0, 0.8)]))
    old = build_song_order_question(
        SongAlignment.model_validate(alignment), ["a", "b"], question_id="old"
    ).model_copy(update={"song_generation": GEN - 1})
    events = [
        ("assistant", {"song_order_question": old.model_dump(mode="json")}),
        ("user", {"song_order": {"question_id": "old", "ordered_media_ids": ["a", "b"]}}),
    ]
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events))
    result = await _gate(_Db(_item(alignment)), _manifest("a", "b"), _strategy())
    assert result.resolved_takes is None
    assert result.plan is not None
    assert result.plan.song_order_question.song_generation == GEN


async def test_answer_for_a_different_take_set_is_asked_again(monkeypatch) -> None:
    alignment = _alignment(_conf("a", 10.0), _amb("b", 30.0, [(30.0, 0.9)]))
    old = build_song_order_question(
        SongAlignment.model_validate(_alignment(_amb("a", 1.0, []), _amb("z", 2.0, []))),
        ["a", "z"],
        question_id="old",
    )
    events = [
        ("assistant", {"song_order_question": old.model_dump(mode="json")}),
        ("user", {"song_order": {"question_id": "old", "ordered_media_ids": ["z", "a"]}}),
    ]
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events))
    result = await _gate(_Db(_item(alignment)), _manifest("a", "b"), _strategy())
    assert result.plan is not None and result.plan.song_order_question is not None


async def test_uncertain_take_with_no_fitting_alternate_becomes_broll(monkeypatch) -> None:
    alignment = _alignment(_conf("a", 10.0), _amb("b", 90.0, [(90.0, 0.9)]), _conf("c", 20.0))
    q = build_song_order_question(SongAlignment.model_validate(alignment), ["a", "b", "c"])
    events = [
        ("assistant", {"song_order_question": q.model_dump(mode="json")}),
        (
            "user",
            {"song_order": {"question_id": q.question_id, "ordered_media_ids": ["a", "b", "c"]}},
        ),
    ]
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events))
    result = await _gate(_Db(_item(alignment)), _manifest("a", "b", "c"), _strategy())
    broll = next(t for t in result.resolved_takes if t["media_id"] == "b")
    assert broll["delta_s"] is None and broll["status"] == "unmatched"


async def test_failed_song_analysis_proceeds_without_asking() -> None:
    # Every take is unmatched: an order question could not place anything.
    db = _Db(_item(None, analysis={"status": "failed"}))
    result = await _gate(db, _manifest("a", "b"), _strategy())
    assert result.plan is None and result.resolved_takes is None


async def test_only_unmatched_takes_ask_nothing_and_proceed() -> None:
    unmatched = [TakeAlignment(media_id=m, status="unmatched") for m in ("a", "b", "c")]
    result = await _gate(_Db(_item(_alignment(*unmatched))), _manifest("a", "b", "c"), _strategy())
    assert result.plan is None and result.resolved_takes is None


async def test_confident_plus_unmatched_asks_nothing() -> None:
    rows = [_conf("a", 5.0), TakeAlignment(media_id="b", status="unmatched")]
    result = await _gate(_Db(_item(_alignment(*rows))), _manifest("a", "b"), _strategy())
    assert result.plan is None


async def test_mixed_ambiguous_and_unmatched_asks_with_every_take_listed() -> None:
    rows = [
        _conf("a", 5.0),
        _amb("b", 30.0, [(30.0, 0.9), (80.0, 0.8)]),
        TakeAlignment(media_id="c", status="unmatched"),
    ]
    result = await _gate(_Db(_item(_alignment(*rows))), _manifest("a", "b", "c"), _strategy())
    q = result.plan.song_order_question
    assert set(q.proposed_order) == {"a", "b", "c"}
    assert "one of your takes" in result.plan.response


async def test_stale_alignment_version_enqueues_one_realignment(monkeypatch) -> None:
    from app.tasks import user_song

    enqueue = Mock()
    monkeypatch.setattr(user_song, "enqueue_user_song_alignment", enqueue)
    monkeypatch.setattr(settings, "song_alignment_turn_deadline_s", 0.05)
    stale = {**_alignment(_conf("a", 1.0)), "version": 1}
    db = _Db(_item(stale, analysis={"status": "ready"}))
    result = await _gate(db, _manifest("a"), _strategy())
    assert result.plan is not None and result.plan.turn_value == "recovery"
    assert db.gets >= 2  # polled repeatedly, yet enqueued once
    enqueue.assert_called_once_with(ITEM)


async def test_selected_media_ids_narrow_the_takes_and_assets_are_not_takes() -> None:
    manifest = SimpleNamespace(
        media=_media("a", "b", "asset-stock") + [SimpleNamespace(media_id="img", kind="image")],
        manifest_hash="m",
        context_hash="c",
    )
    db = _Db(_item(_alignment(_conf("a", 1.0))))
    result = await _gate(db, manifest, _strategy(selected_media_ids=["a"]))
    assert result.plan is None  # only `a` is a take, and it is confident


# -- gating: when the gate must not engage -------------------------------------------


@pytest.mark.parametrize(
    "strategy",
    [
        SimpleNamespace(),  # no song fields at all (pre-Lane-C strategy)
        _strategy(song_sync="background"),
        _strategy(audio_strategy="licensed_music"),
        _strategy(song_sync=None),
    ],
)
async def test_non_lipsync_strategies_never_touch_the_db(strategy) -> None:
    db = SimpleNamespace()  # any attribute access would raise
    result = await _gate(db, _manifest("a"), strategy)
    assert result.plan is None and result.resolved_takes is None


async def test_flag_off_is_a_passthrough(monkeypatch) -> None:
    monkeypatch.setattr(settings, "user_song_montage_enabled", False)
    result = await _gate(SimpleNamespace(), _manifest("a"), _strategy())
    assert result.plan is None and result.resolved_takes is None


# -- wiring through _plan_from_creator_output ----------------------------------------


def _propose() -> ProposeStrategy:
    return ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="guided_story",
            edit_format="day_vlog",
            audio_strategy="licensed_music",
            pacing="fast",
            render_program="guided",
            selected_media_ids=[],
            rationale="Build from the morning setup to the finished reveal.",
        ),
        summary="Open on the whisk and end on the product.",
    )


async def _call(db, action):  # noqa: ANN001, ANN202
    return await _plan_from_creator_output(
        db or SimpleNamespace(),
        thread_id=THREAD,
        item_id=ITEM,
        creator_id=CREATOR,
        user_message="make it",
        manifest=_manifest("a"),
        inputs=SimpleNamespace(intent_clips=[], creator_request="make it"),
        output=SimpleNamespace(action=action),
        brief_request=None,
    )


def _patch_policy(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(
        planner,
        "check_strategy_for_runtime_v2",
        lambda _m, strategy: CheckedStrategy(strategy=strategy, notices=()),
    )


async def test_no_song_strategy_output_is_byte_identical(monkeypatch) -> None:
    _patch_policy(monkeypatch)
    action = _propose()
    planned = await _call(SimpleNamespace(), action)
    expected = adapt_creator_action(action)
    assert planned.plan.model_dump(mode="json") == expected.model_dump(mode="json")
    assert "song_order_question" not in planned.plan.model_dump(mode="json")


async def test_gate_question_short_circuits_the_planner_turn(monkeypatch) -> None:
    _patch_policy(monkeypatch)
    monkeypatch.setattr(planner, "_is_lipsync_song_strategy", lambda _s: True)
    db = _Db(_item(_alignment(_amb("a", 5.0, [(5.0, 0.9), (50.0, 0.8)]))))
    planned = await _call(db, _propose())
    assert planned.plan.mode == "respond"
    assert planned.plan.song_order_question is not None
    assert planned.manifest_hash == "m" * 64


async def test_resolved_takes_are_written_onto_the_strategy_for_the_draft(monkeypatch) -> None:
    _patch_policy(monkeypatch)
    resolved = [
        {"media_id": "a", "delta_s": 5.0, "status": "confident", "confirmed_by_creator": True}
    ]

    async def _gate_stub(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return planner._SongGateResult(resolved_takes=resolved)

    monkeypatch.setattr(planner, "_song_order_gate", _gate_stub)
    seen = {}

    def _spy(action, **kw):  # noqa: ANN001, ANN003, ANN202
        seen["strategy"] = action.strategy
        return adapt_creator_action(action, **kw)

    monkeypatch.setattr(planner, "adapt_creator_action", _spy)
    await _call(SimpleNamespace(), _propose())
    assert getattr(seen["strategy"], "resolved_song_takes", None) == resolved


# -- the answer turn re-runs the model, which may drop lip-sync -------------------------


def _answered_events(alignment: dict, *, generation: int | None = GEN, answered: bool = True):
    q = build_song_order_question(SongAlignment.model_validate(alignment), ["a", "b"])
    if generation != GEN:
        q = q.model_copy(update={"song_generation": generation})
    events = [("assistant", {"song_order_question": q.model_dump(mode="json")})]
    if answered:
        events.append(
            (
                "user",
                {"song_order": {"question_id": q.question_id, "ordered_media_ids": ["b", "a"]}},
            )
        )
    return events


async def test_a_flipped_song_sync_on_the_answer_turn_still_applies_the_answer(monkeypatch) -> None:
    """Probe: the model said `background` on the answer turn, the gate was skipped and the
    creator's confirmed order was silently dropped."""
    alignment = _alignment(_conf("a", 10.0), _amb("b", 80.0, [(5.0, 0.9), (80.0, 0.8)]))
    events = _answered_events(alignment)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events))
    flipped = _strategy(song_sync="background")

    result = await _gate(_Db(_item(alignment)), _manifest("a", "b"), flipped)

    assert result.plan is None
    assert result.strategy is not None and result.strategy.song_sync == "lipsync"
    assert [t["media_id"] for t in result.resolved_takes] == ["b", "a"]


async def test_a_flipped_song_sync_with_an_open_question_asks_it_again(monkeypatch) -> None:
    alignment = _alignment(_conf("a", 10.0), _amb("b", 80.0, [(5.0, 0.9), (80.0, 0.8)]))
    events = _answered_events(alignment, answered=False)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events))

    result = await _gate(_Db(_item(alignment)), _manifest("a", "b"), _strategy(song_sync=None))

    assert result.plan is not None and result.plan.song_order_question is not None


async def test_a_later_background_request_is_not_overridden(monkeypatch) -> None:
    alignment = _alignment(_conf("a", 10.0), _amb("b", 80.0, [(5.0, 0.9), (80.0, 0.8)]))
    events = [
        *_answered_events(alignment),
        ("user", {"text": "actually just use it as background music"}),
    ]
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events))

    result = await _gate(
        _Db(_item(alignment)), _manifest("a", "b"), _strategy(song_sync="background")
    )

    assert result.plan is None and result.strategy is None and result.resolved_takes is None


async def test_a_flip_after_the_song_was_replaced_is_not_overridden(monkeypatch) -> None:
    alignment = _alignment(_conf("a", 10.0), _amb("b", 80.0, [(5.0, 0.9), (80.0, 0.8)]))
    events = _answered_events(alignment, generation=GEN - 1)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events))

    result = await _gate(
        _Db(_item(alignment)), _manifest("a", "b"), _strategy(song_sync="background")
    )

    assert result.plan is None and result.strategy is None


async def test_the_planner_recompiles_the_kept_lipsync_strategy(monkeypatch) -> None:
    _patch_policy(monkeypatch)
    kept = _propose().strategy.model_copy(update={"song_sync": "lipsync"})
    seen = []

    def _check(_m, strategy):  # noqa: ANN001, ANN202
        seen.append(strategy)
        return CheckedStrategy(strategy=strategy, notices=("extra notice.",))

    monkeypatch.setattr(planner, "check_strategy_for_runtime_v2", _check)

    async def _gate_stub(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return planner._SongGateResult(strategy=kept)

    monkeypatch.setattr(planner, "_song_order_gate", _gate_stub)
    adapted = {}

    def _spy(action, **kw):  # noqa: ANN001, ANN003, ANN202
        adapted["action"] = action
        return adapt_creator_action(action, **kw)

    monkeypatch.setattr(planner, "adapt_creator_action", _spy)
    await _call(SimpleNamespace(), _propose())
    assert seen[-1] is kept
    assert adapted["action"].strategy is kept
    assert "extra notice." in adapted["action"].summary
