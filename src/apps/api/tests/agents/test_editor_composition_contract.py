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


@pytest.mark.parametrize("has_supported_change", [False, True])
def test_unmet_requests_are_disclosed_even_when_model_calls_them_an_edit(has_supported_change):
    request = EditCopilotInput(
        utterance="Make the title yellow and replace my speech with a Japanese voice.",
        variant_snapshot={
            "editor_ops_version": 2,
            "allowed_op_families": ["text"],
            "total_duration_s": 5,
            "text_bars": [{"id": "title", "text": "Hello", "start_s": 0, "end_s": 5}],
        },
    )
    output = EditCopilotAgent(ModelClient()).parse(
        json.dumps(
            {
                "intent": "edit",
                "confidence": 1,
                "reply": "Done, all changes are ready.",
                "ops": [{"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#FFFF00"}}]
                if has_supported_change
                else [],
                "unmet_requests": [
                    {"request": "Replace my speech", "reason": "Voice replacement is unavailable."}
                ],
            }
        ),
        request,
    )
    assert "Voice replacement is unavailable." in output.reply
    assert "all changes" not in output.reply
    assert output.outcome == ("proposed" if has_supported_change else "unsupported")


def test_partial_supported_reply_respects_turkish_and_preserves_the_limitation():
    from app.kria.reply_language import reply_language_for

    request = EditCopilotInput(
        utterance="Başlığı sarı yap ve konuşmamı Japonca sesle değiştir.",
        reply_language="tr",
        variant_snapshot={
            "editor_ops_version": 2,
            "allowed_op_families": ["text"],
            "total_duration_s": 5,
            "text_bars": [{"id": "title", "text": "Merhaba", "start_s": 0, "end_s": 5}],
        },
    )
    with reply_language_for("tr"):
        output = EditCopilotAgent(ModelClient()).parse(
            json.dumps(
                {
                    "intent": "edit",
                    "confidence": 1,
                    "reply": "Hepsini yaptım.",
                    "ops": [
                        {"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#FFFF00"}}
                    ],
                    "unmet_requests": [
                        {
                            "request": "Konuşmamı değiştir",
                            "reason": "Ses değiştirme desteklenmiyor.",
                        }
                    ],
                }
            ),
            request,
        )
    assert output.outcome == "proposed"
    assert len(output.ops) == 1
    assert output.reply == "Desteklenen değişiklikleri hazırladım. Ses değiştirme desteklenmiyor."
