"""Tappable answers for a length question (KRI-738).

The copilot already offered 2-3 lengths in prose, so the creator had to retype one. It now also
records them as structured ``length_options``; ``length_options()`` keeps an option only when the
question the creator can read states the same length, so a chip never offers a number that was
not shown above it.
"""

from __future__ import annotations

import pytest

from app.agents.length_options import length_options
from app.kria.contracts import KriaTurnPlan

QUESTION = (
    'The clips during the "to the stadium" text are about 1.3 seconds each right now. '
    "How short should they be: about 1 second, about 0.8 seconds, or about 0.5 seconds? "
    "Or tell me a number of seconds."
)
THREE = [
    {"label": "About 1 second", "seconds": 1},
    {"label": "About 0.8 seconds", "seconds": 0.8},
    {"label": "About 0.5 seconds", "seconds": 0.5},
]


def _labels(options: list[dict]) -> list[str]:
    return [option["label"] for option in options]


def test_the_aston_villa_question_becomes_three_options() -> None:
    assert _labels(length_options(THREE, QUESTION)) == [
        "About 1 second",
        "About 0.8 seconds",
        "About 0.5 seconds",
    ]


def test_an_option_whose_length_the_question_never_states_is_dropped() -> None:
    # The server swaps the model's reply for its own text on some turns; stale options must not
    # survive it.
    assert length_options(THREE, "How short should the clips be?") == []
    seven = [*THREE[:2], {"label": "About 7 seconds", "seconds": 7}]
    assert _labels(length_options(seven, QUESTION)) == ["About 1 second", "About 0.8 seconds"]


def test_one_option_is_not_a_choice() -> None:
    assert length_options(THREE[:1], QUESTION) == []


@pytest.mark.parametrize(
    "junk",
    [None, "About 1 second", 5, {"label": "x", "seconds": 1}, [], [None, 3, "x"], [{}], [{"a": 1}]],
)
def test_malformed_options_never_raise(junk) -> None:
    assert length_options(junk, QUESTION) == []


@pytest.mark.parametrize("seconds", [True, "1", None, 0.0, 0.05, 121, -1])
def test_a_seconds_value_that_is_not_a_real_length_is_dropped(seconds) -> None:
    bad = [{"label": "About 1 second", "seconds": seconds}, THREE[1], THREE[2]]

    assert _labels(length_options(bad, QUESTION)) == ["About 0.8 seconds", "About 0.5 seconds"]


def test_duplicates_blank_and_long_labels_are_dropped_and_the_list_is_capped() -> None:
    reply = "Pick 1 second, 2 seconds, 3 seconds, 4 seconds, 5 seconds or 6 seconds."
    raw = [
        {"label": "1 second", "seconds": 1},
        {"label": "1 SECOND", "seconds": 1},
        {"label": "  ", "seconds": 2},
        {"label": "2 seconds", "seconds": 2},
        {"label": "3 seconds", "seconds": 3},
        {"label": "4 seconds", "seconds": 4},
        {"label": "5 seconds", "seconds": 5},
        {"label": "x" * 80, "seconds": 6},
    ]

    assert _labels(length_options(raw, reply)) == [
        "1 second",
        "2 seconds",
        "3 seconds",
        "4 seconds",
    ]


def test_a_comma_decimal_reply_matches() -> None:
    reply = "Şu an klipler 1,3 saniye. 0,8 saniye mi, 0,5 saniye mi?"
    raw = [{"label": "0,8 saniye", "seconds": 0.8}, {"label": "0,5 saniye", "seconds": 0.5}]

    assert _labels(length_options(raw, reply)) == ["0,8 saniye", "0,5 saniye"]


def test_a_plan_without_options_dumps_exactly_as_before() -> None:
    plan = KriaTurnPlan(mode="respond", turn_value="question", response="Which style?")

    assert "reply_options" not in plan.model_dump(mode="json")
    with_options = KriaTurnPlan(
        mode="respond",
        turn_value="question",
        response=QUESTION,
        reply_options=["About 1 second", "About 0.8 seconds"],
    )
    assert with_options.model_dump(mode="json")["reply_options"] == [
        "About 1 second",
        "About 0.8 seconds",
    ]
