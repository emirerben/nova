"""The copilot's length question carries tappable options (KRI-738)."""

from __future__ import annotations

import json

from app.agents.edit_copilot import EditCopilotInput
from tests.test_edit_copilot import _agent, _snapshot

QUESTION = (
    "The clips are about 3 seconds each right now. How short should they be: about 2 seconds, "
    "about 1 second, or about 0.5 seconds? Or tell me a number of seconds."
)
OPTIONS = [
    {"label": "About 2 seconds", "seconds": 2},
    {"label": "About 1 second", "seconds": 1},
    {"label": "About 0.5 seconds", "seconds": 0.5},
]


def _parse(payload: dict):
    body = {"confidence": 0.9, **payload}
    return _agent().parse(
        json.dumps(body),
        EditCopilotInput(utterance="make the clips shorter", variant_snapshot=_snapshot()),
    )


def test_a_length_question_keeps_the_options_it_states() -> None:
    out = _parse(
        {
            "intent": "clarify",
            "ops": [],
            "reply": QUESTION,
            "needs_clarification": True,
            "suggestions": ["Make each clip 2 seconds", "Make each clip 1 second"],
            "length_options": OPTIONS,
        }
    )

    assert out.outcome == "clarification"
    assert [option["label"] for option in out.length_options] == [
        "About 2 seconds",
        "About 1 second",
        "About 0.5 seconds",
    ]


def test_an_option_the_question_does_not_state_is_dropped() -> None:
    out = _parse(
        {
            "intent": "clarify",
            "ops": [],
            "reply": QUESTION,
            "needs_clarification": True,
            "length_options": [*OPTIONS[:2], {"label": "About 9 seconds", "seconds": 9}],
        }
    )

    assert [option["seconds"] for option in out.length_options] == [2, 1]


def test_a_turn_that_edits_never_carries_options() -> None:
    out = _parse(
        {
            "intent": "edit",
            "ops": [
                {
                    "op": "patch_slots",
                    "selector": {"all": True},
                    "patch": {"duration_s": 1},
                }
            ],
            "reply": "Made each clip 1 second.",
            "needs_clarification": False,
            "length_options": OPTIONS,
        }
    )

    assert out.length_options == []


def test_no_options_means_the_output_dumps_exactly_as_before() -> None:
    out = _parse({"intent": "clarify", "ops": [], "reply": QUESTION, "needs_clarification": True})

    assert out.length_options == []
    assert "length_options" not in out.model_dump(mode="json")
