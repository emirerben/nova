from __future__ import annotations

import pytest

from app.agents.edit_copilot import EditorOperationParseState, parse_editor_operation


def snapshot():
    return {
        "allowed_op_families": ["visual_media", "text"],
        "visual_media": [{"id": "photo-1"}, {"id": "video-2"}],
        "text_bars": [{"id": "player-name", "text": "Alex"}],
    }


def test_uploaded_visual_ids_are_resolved_without_text_indices():
    op = {"op": "remove_visual_media", "target_ids": ["photo-1", "video-2"]}
    assert parse_editor_operation(op, snapshot(), EditorOperationParseState(0.95)) == op


@pytest.mark.parametrize(
    "targets",
    [[], ["player-name"], ["photo-1", "unknown"], ["photo-1", "photo-1"], [True], "photo-1"],
)
def test_invalid_visual_bundle_is_rejected_atomically(targets):
    assert (
        parse_editor_operation(
            {"op": "remove_visual_media", "target_ids": targets},
            snapshot(),
            EditorOperationParseState(0.95),
        )
        is None
    )


@pytest.mark.parametrize(
    "families", [None, [], ["all"], ["visual"], ["overlay"], ["remove_visual_media"]]
)
def test_older_editor_capabilities_cannot_expose_server_media_removal(families):
    current = {**snapshot(), "allowed_op_families": families}
    assert (
        parse_editor_operation(
            {"op": "remove_visual_media", "target_ids": ["photo-1"]},
            current,
            EditorOperationParseState(0.95),
        )
        is None
    )


@pytest.mark.parametrize(
    "reply",
    ["I saved those changes.", "I've deleted all the visuals.", "Cleared the uploaded images."],
)
def test_no_operation_cannot_claim_saved_or_deleted(reply):
    from app.agents.edit_copilot import EditCopilotOutput
    from app.routes._copilot import _honest_outcome

    output = EditCopilotOutput(intent="edit", ops=[], confidence=0.9, reply=reply)
    outcome, message = _honest_outcome(output, [])
    assert outcome == "no_effect"
    assert message != reply
