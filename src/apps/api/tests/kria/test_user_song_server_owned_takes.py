"""KRI-374 D2: ``resolved_song_takes`` is server-owned.

Only the song-order gate may write it (after the creator answered). A model-authored
strategy, the creator-agent route's hygiene and ``adapt_creator_action`` all discard
whatever arrives with the strategy; the gate's own explicit path is the one that survives.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.kria import planner
from app.kria.planner import adapt_creator_action
from app.routes.creator_agent import _model_strategy_hygiene

FORGED = [{"media_id": "a", "delta_s": 3.0, "status": "confident", "confirmed_by_creator": True}]
RESOLVED = [{"media_id": "b", "delta_s": 9.5, "status": "confident", "confirmed_by_creator": True}]


def _strategy(**changes) -> CreativeStrategy:
    return CreativeStrategy(
        edit_format="montage",
        audio_strategy="user_song",
        render_program="guided",
        song_sync="lipsync",
        **changes,
    )


def _action(**changes) -> ProposeStrategy:
    return ProposeStrategy(
        kind="propose_strategy", summary="Lip-sync to your song.", strategy=_strategy(**changes)
    )


def _applied_strategy(plan) -> dict:
    intent = next(i for i in plan.intents if i.tool_name == "draft.apply_strategy")
    return intent.arguments["strategy"]


def test_creator_agent_route_hygiene_discards_a_model_authored_takes_list() -> None:
    strategy = _strategy().model_copy(update={"resolved_song_takes": FORGED})
    assert strategy.resolved_song_takes == FORGED
    assert _model_strategy_hygiene(strategy).resolved_song_takes is None


def test_hygiene_leaves_a_strategy_without_takes_untouched(monkeypatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    clean = _strategy()
    assert _model_strategy_hygiene(clean).model_dump(mode="json") == clean.model_dump(mode="json")


def test_adapt_creator_action_drops_takes_carried_by_the_action() -> None:
    action = _action().model_copy(
        update={"strategy": _strategy().model_copy(update={"resolved_song_takes": FORGED})}
    )
    assert "resolved_song_takes" not in _applied_strategy(adapt_creator_action(action))


def test_adapt_creator_action_keeps_only_the_gates_explicit_takes() -> None:
    forged = _action().model_copy(
        update={"strategy": _strategy().model_copy(update={"resolved_song_takes": FORGED})}
    )
    plan = adapt_creator_action(forged, server_resolved_song_takes=RESOLVED)
    assert _applied_strategy(plan)["resolved_song_takes"] == RESOLVED


@pytest.mark.asyncio
async def test_the_answered_gate_path_still_reaches_the_draft(monkeypatch) -> None:
    """The happy path: gate resolves the creator's answer -> the applied strategy has it."""

    async def gate(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return planner._SongGateResult(resolved_takes=RESOLVED)

    monkeypatch.setattr(planner, "_song_order_gate", gate)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    from tests.kria.test_planner_song_order import _call, _patch_policy

    _patch_policy(monkeypatch)
    # The model tried to forge takes; the gate's list is what must come out.
    action = _action().model_copy(
        update={"strategy": _strategy().model_copy(update={"resolved_song_takes": FORGED})}
    )
    planned = await _call(SimpleNamespace(), action)
    assert _applied_strategy(planned.plan)["resolved_song_takes"] == RESOLVED


@pytest.mark.asyncio
async def test_a_turn_with_no_gate_answer_carries_no_takes(monkeypatch) -> None:
    async def gate(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return planner._SongGateResult()

    monkeypatch.setattr(planner, "_song_order_gate", gate)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    from tests.kria.test_planner_song_order import _call, _patch_policy

    _patch_policy(monkeypatch)
    action = _action().model_copy(
        update={"strategy": _strategy().model_copy(update={"resolved_song_takes": FORGED})}
    )
    planned = await _call(SimpleNamespace(), action)
    assert "resolved_song_takes" not in _applied_strategy(planned.plan)
