"""Cross-layer editor contracts, independent of any particular request wording."""

import json

import pytest

from app.agents._runtime import ModelClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput, _resolve_placement
from app.services.editor_limits import MAX_EDITOR_OPS


@pytest.mark.parametrize("patch", [{"x_frac": 0.12}, {"y_frac": 0.86}])
def test_partial_coordinate_patch_does_not_invent_the_other_axis(patch):
    assert _resolve_placement(patch) == {**patch, "position": "custom"}


def test_explicit_position_preset_still_supplies_its_anchor():
    assert _resolve_placement({"position": "bottom", "x_frac": 0.12}) == {
        "position": "custom",
        "x_frac": 0.12,
        "y_frac": 0.85,
    }


def test_normalized_position_alias_and_coordinate_compose():
    assert _resolve_placement({"position": "center", "x_frac": 0.12}) == {
        "position": "custom",
        "x_frac": 0.12,
        "y_frac": 0.5,
    }


def test_server_editor_parser_uses_the_compiler_operation_bound():
    request = EditCopilotInput(
        utterance="Add sequential text layers.",
        variant_snapshot={
            "editor_ops_version": 2,
            "allowed_op_families": ["text"],
            "total_duration_s": 30,
            "text_bars": [],
        },
    )
    output = EditCopilotAgent(ModelClient()).parse(
        json.dumps(
            {
                "intent": "edit",
                "reply": "Prepared the text layers.",
                "confidence": 1,
                "ops": [
                    {"op": "add_text", "text": str(i), "start_s": i, "end_s": i + 1}
                    for i in range(MAX_EDITOR_OPS + 1)
                ],
            }
        ),
        request,
    )
    assert not output.ops


def test_unsupported_reply_surfaces_the_itemized_limitation():
    output = EditCopilotAgent(ModelClient()).parse(
        json.dumps(
            {
                "intent": "reject",
                "ops": [],
                "confidence": 1,
                "reply": "Use the caption controls on the video page.",
                "unmet_requests": [
                    {
                        "request": "Replace the spoken audio with a different language.",
                        "reason": (
                            "Speech translation and voice replacement are not supported in chat."
                        ),
                    }
                ],
            }
        ),
        EditCopilotInput(utterance="Replace the spoken audio", variant_snapshot={}),
    )
    assert output.reply == "Speech translation and voice replacement are not supported in chat."
    assert output.ops == []
