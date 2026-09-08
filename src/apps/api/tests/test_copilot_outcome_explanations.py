"""An unsupported edit must retain its reason without claiming a change."""

import pytest

from app.agents.edit_copilot import EditCopilotOutput
from app.routes._copilot import _honest_outcome


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            "I cannot change score timing with the operations available to this editor.",
            "I cannot change score timing with the operations available to this editor.",
        ),
        ("Done. I changed the scores.", "That kind of edit isn't available for this draft yet."),
        (
            "I changed the title but not the timing.",
            "That kind of edit isn't available for this draft yet.",
        ),
        ("", "That kind of edit isn't available for this draft yet."),
    ],
)
def test_unsupported_preserves_explanation_without_success_claim(reply: str, expected: str) -> None:
    output = EditCopilotOutput(intent="reject", ops=[], confidence=0.9, reply=reply)

    outcome, response = _honest_outcome(output, [])

    assert outcome == "unsupported"
    assert response == expected


def test_structured_rejection_remains_authoritative() -> None:
    output = EditCopilotOutput(
        intent="reject",
        ops=[],
        confidence=0.9,
        reply="I cannot find the score labels.",
        rejection_reasons=[
            {"op": "edit_text", "reason": "capability_unavailable", "detail": "Text is locked."}
        ],
    )

    assert _honest_outcome(output, []) == ("unsupported", "Text is locked.")
