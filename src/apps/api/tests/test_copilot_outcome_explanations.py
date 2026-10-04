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


def test_no_effect_with_unmet_reason_never_claims_already_reflected() -> None:
    output = EditCopilotOutput(
        intent="edit",
        ops=[],
        confidence=0.9,
        reply="Done, I deleted the Atlantis labels.",
        unmet_requests=[
            {
                "request": "delete all the labels that say Atlantis",
                "reason": "No labels matching 'Atlantis' were found in the current draft.",
            }
        ],
    )

    outcome, response = _honest_outcome(output, [])

    assert outcome == "no_effect"
    assert response == "No labels matching 'Atlantis' were found in the current draft."


_CAP_REJECTION = [
    {
        "op": "patch_slots",
        "reason": "capability_unavailable",
        "detail": "I can't change that on this edit yet.",
    }
]


@pytest.mark.parametrize(
    "reply",
    [
        "I shortened all the clips to make the video faster.",
        "Shortening every clip now, the video is faster.",
        "I trimmed and reordered the clips, but not the music.",
    ],
)
def test_capability_rejection_never_surfaces_success_prose(reply: str) -> None:
    output = EditCopilotOutput(
        intent="edit", ops=[], confidence=0.9, reply=reply, rejection_reasons=_CAP_REJECTION
    )

    outcome, response = _honest_outcome(output, [])

    assert outcome == "unsupported"
    assert response == "I can't change that on this edit yet."


@pytest.mark.parametrize("outcome_reason", ["missing_required", "other"])
def test_failed_or_no_effect_rejection_replaces_success_claim(outcome_reason: str) -> None:
    output = EditCopilotOutput(
        intent="edit",
        ops=[],
        confidence=0.9,
        reply="I shortened all the clips.",
        rejection_reasons=[{"op": "patch_slots", "reason": outcome_reason, "detail": "No slots."}],
    )

    _outcome, response = _honest_outcome(output, [])

    assert "shortened" not in response


@pytest.mark.parametrize(
    "reply",
    [
        "I didn't shorten any clips.",
        "Nothing was changed.",
        "I can't shorten the clips on this edit yet.",
    ],
)
def test_negated_phrasing_passes_through_when_nothing_to_contradict(reply: str) -> None:
    output = EditCopilotOutput(intent="edit", ops=[], confidence=0.9, reply=reply)

    outcome, response = _honest_outcome(output, [])

    assert outcome == "no_effect"
    assert response == reply


def test_genuine_success_with_ops_is_untouched() -> None:
    output = EditCopilotOutput(intent="edit", ops=[], confidence=0.9, reply="Tightened the cuts.")
    ops = [{"op": "patch_slots"}]

    assert _honest_outcome(output, ops) == (
        "proposed",
        "I prepared this edit for the editor to validate and stage.",
    )
    output = EditCopilotOutput(
        intent="edit", ops=[], confidence=0.9, reply="Sounds good, a tighter pace works."
    )
    assert _honest_outcome(output, ops)[1] == "Sounds good, a tighter pace works."


def test_unsupported_overlay_display_ask_names_the_real_limit() -> None:
    from app.routes._copilot import OVERLAY_DISPLAY_LIMIT_REPLY, is_overlay_display_ask

    output = EditCopilotOutput(intent="reject", ops=[], confidence=0.9, reply="")
    outcome, response = _honest_outcome(output, [], message="Use all overlays as full screen.")
    assert outcome == "unsupported"
    assert response == OVERLAY_DISPLAY_LIMIT_REPLY
    assert "fresh edit" in response
    # Unrelated asks keep the generic fallback.
    _, other = _honest_outcome(output, [], message="make the text bigger")
    assert other == "That kind of edit isn't available for this draft yet."
    assert is_overlay_display_ask("switch between the overlays full-screen")
    assert not is_overlay_display_ask("make the captions full screen")
