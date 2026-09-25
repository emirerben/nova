"""Multi-turn replay: state carries from turn to turn (KRI-185 P6a)."""

from pathlib import Path

import pytest

from app.kria.replay import load_fixture, replay_fixture, replay_thread

FIXTURES = Path(__file__).parents[1] / "fixtures" / "kria_turns"


def _turns():
    return [
        load_fixture(FIXTURES / "nermin-matcha-update.json"),
        load_fixture(FIXTURES / "nermin-travel-diary.json"),
    ]


def test_each_turn_still_replays_exactly_like_a_single_turn():
    matcha, travel = _turns()
    trace = replay_thread("t", [matcha, travel])
    assert [t.fixture_id for t in trace.traces] == [matcha.fixture_id, travel.fixture_id]
    assert trace.traces[0].response == replay_fixture(matcha).response
    assert trace.traces[1].response.message == replay_fixture(travel).response.message


def test_conversation_accumulates_user_and_assistant_messages():
    matcha, travel = _turns()
    trace = replay_thread("t", [matcha, travel])
    conversation = trace.final_snapshot["conversation"]
    assert [c["role"] for c in conversation] == ["user", "assistant", "user", "assistant"]
    assert conversation[0]["content"] == matcha.user_message
    assert conversation[1]["content"] == trace.traces[0].response.message
    assert conversation[3]["content"] == trace.traces[1].response.message


def test_a_later_turn_sees_the_earlier_conversation_and_overrides_only_what_it_states():
    matcha, travel = _turns()
    seen = {}

    from app.kria.registry import KRIA_TOOLS

    class Spy:
        def get(self, name, version):
            registered = KRIA_TOOLS.get(name, version)
            handler = registered.handler

            def spying(args, snapshot):
                seen[len(seen)] = dict(snapshot)
                return handler(args, snapshot)

            return type(registered)(**{**registered.__dict__, "handler": spying})

    replay_thread("t", [matcha, travel], registry=Spy())
    assert seen[0]["conversation"] == []
    assert len(seen[1]["conversation"]) == 2
    assert seen[1]["media_labels"] == travel.snapshot["media_labels"]


def test_snapshot_keys_carry_forward_when_a_later_turn_omits_them():
    matcha, travel = _turns()
    sparse = travel.model_copy(update={"snapshot": {"goal": "trip"}, "expected_message": None})
    trace = replay_thread("t", [matcha, sparse])
    assert trace.final_snapshot["media_labels"] == matcha.snapshot["media_labels"]
    assert trace.final_snapshot["goal"] == "trip"


def test_thread_revision_may_not_move_backwards():
    matcha, travel = _turns()
    later = matcha.model_copy(update={"current_thread_revision": 3})
    earlier = travel.model_copy(update={"current_thread_revision": 2})
    with pytest.raises(ValueError, match="backwards"):
        replay_thread("t", [later, earlier])


def test_an_empty_thread_replays_to_an_empty_trace():
    trace = replay_thread("t", [])
    assert trace.traces == [] and trace.final_snapshot == {"conversation": []}
