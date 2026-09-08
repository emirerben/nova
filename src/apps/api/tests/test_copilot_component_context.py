from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput, _format_snapshot


def _negotiated_snapshot() -> dict:
    return {
        "component_context_version": 1,
        "asset_context_status": "loading",
        "editor_focus": {
            "playhead_s": 1.25,
            "selected": {"kind": "caption", "id": "cue-40", "index": 40},
        },
        "allowed_op_families": ["text"],
        "total_duration_s": 60,
        "has_narrated_captions": True,
        "source_assets": [
            {
                "clip_index": 0,
                "media_id": "source-0",
                "kind": "image",
                "used": True,
                "status": "ready",
                "context": {
                    "semantic_role": "hero subject",
                    "description": "A full grounded description that remains intact "
                    "past the old short asset preview limit.",
                    "user_context": "creator supplied context",
                    "on_screen_text": "label from OCR",
                },
            },
            {
                "clip_index": 1,
                "media_id": "source-1",
                "kind": "video",
                "used": False,
                "status": "ready",
                "context": {"subject": "unused source"},
            },
        ],
        "text_bars": [
            {
                "id": "text-0",
                "role": "overlay_text",
                "text": "hook",
                "start_s": 0,
                "end_s": 2,
                "context": {
                    "semantic_role": "pricing_badge",
                    "source": "user_text",
                    "source_text": "pricing copy",
                },
            }
        ],
        "slots": [
            {
                "media_id": "source-0",
                "media_kind": "image",
                "output_start_s": 0,
                "output_end_s": 2,
                "duration_s": 2,
                "context": {"asset_id": "source-0", "subject": "hero subject"},
            }
        ],
        "camera_effects": [
            {"start_s": 0, "end_s": 1, "intensity": 0.5, "context": {"group_id": "camera-1"}}
            for _ in range(21)
        ],
        "visual_blocks": [
            {
                "id": f"visual-{index}",
                "kind": "media",
                "start_s": 0,
                "end_s": 2,
                "transition_in": "cut",
                "transition_out": "cut",
                "details": {
                    "asset_id": "source-0",
                    "media_kind": "image",
                    "display_mode": "fullscreen",
                    "x_frac": 0.5,
                    "y_frac": 0.5,
                    "scale": 1,
                    "z": 2,
                    "transform": {"fit_mode": "cover", "focal_x": 0.5, "focal_y": 0.5, "zoom": 1},
                },
                "context": {"semantic_role": "cutaway", "source": "ai"},
            }
            for index in range(21)
        ],
        "motion": {
            "catalog": [{"preset_id": f"preset-{index}"} for index in range(13)],
            "blocks": [
                {
                    "id": f"motion-{index}",
                    "preset_id": "kinetic_word",
                    "preset_version": 2,
                    "label": "Kinetic word",
                    "start_s": 0,
                    "end_s": 1,
                    "params": {},
                    "context": {"semantic_role": "emphasis"},
                }
                for index in range(9)
            ],
            "asset_pool": [
                {
                    "id": f"motion-asset-{index}",
                    "subject": "subject",
                    "context": {"asset_id": f"asset-{index}"},
                }
                for index in range(21)
            ],
        },
        "sfx": {
            "placements": [
                {
                    "index": index,
                    "id": f"sfx-placement-{index}",
                    "label": "click",
                    "at_s": 1,
                    "gain": 1,
                    "duration_s": 0.2,
                    "context": {"semantic_role": "smart_click", "source": "auto_sound_design"},
                }
                for index in range(16)
            ],
            "catalog": [
                {"id": f"sfx-catalog-{index}", "name": "click", "duration_s": 0.2}
                for index in range(21)
            ],
            "suggestions": [
                {"effect_id": f"sfx-suggestion-{index}", "at_s": 1, "gain": 1, "reason": "fit"}
                for index in range(7)
            ],
        },
        "overlays": {
            "cards": [
                {
                    "index": index,
                    "id": f"card-{index}",
                    "kind": "image",
                    "start_s": 1,
                    "end_s": 2,
                    "position": "center",
                    "x_frac": 0.5,
                    "y_frac": 0.5,
                    "scale": 1,
                    "display_mode": "pip",
                    "context": {"semantic_role": "supporting visual", "asset_id": "source-0"},
                }
                for index in range(13)
            ],
            "asset_pool": [
                {
                    "id": f"overlay-asset-{index}",
                    "kind": "image",
                    "subject": "subject",
                    "duration_s": None,
                    "context": {"description": "full asset description"},
                }
                for index in range(13)
            ],
            "pending_suggestions": [
                {"id": f"overlay-suggestion-{index}", "reason": "fit", "start_s": 1, "end_s": 2}
                for index in range(7)
            ],
        },
        "captions": {
            "total_cues": 41,
            "truncated": False,
            "cues_editable": False,
            "cues": [
                {
                    "index": index,
                    "id": f"cue-{index}",
                    "text": f"caption {index}",
                    "start_s": 0,
                    "end_s": 1,
                    "smart_role": "hook",
                    "context": {"semantic_role": "spoken caption", "source": "transcript"},
                }
                for index in range(41)
            ],
            "meta": {"enabled": True},
        },
        "speech": {
            "source": "transcript",
            "words": [
                {"text": f"word-{index}", "start_s": index * 0.1, "end_s": index * 0.1 + 0.05}
                for index in range(151)
            ],
            "pauses": [
                {"start_s": index * 0.1, "end_s": index * 0.1 + 0.02, "after": f"word-{index}"}
                for index in range(41)
            ],
        },
    }


def test_negotiated_context_formats_every_component_row_and_full_speech() -> None:
    rendered = _format_snapshot(_negotiated_snapshot())

    for marker in (
        "source-1",
        "visual-20",
        "preset-12",
        "motion-8",
        "motion-asset-20",
        "sfx-placement-15",
        "sfx-catalog-20",
        "sfx-suggestion-6",
        "card-12",
        "overlay-asset-12",
        "overlay-suggestion-6",
        "cue-40",
        "word-150",
    ):
        assert marker in rendered
    assert rendered.count("(after ") == 41
    assert "id='text-0' role='overlay_text'" in rendered
    assert "context={semantic_role='pricing_badge'" in rendered
    assert "read-only captions: 41 transcript cues are inspectable" in rendered
    assert "their text and timing are not available" not in rendered
    assert "asset_context_status: loading" in rendered
    assert "playhead_s=1.250 selected={kind='caption', id='cue-40', index=40}" in rendered


def test_legacy_formatter_keeps_existing_row_caps() -> None:
    snapshot = _negotiated_snapshot()
    snapshot.pop("component_context_version")
    snapshot.pop("asset_context_status")
    snapshot.pop("editor_focus")
    snapshot.pop("source_assets")
    rendered = _format_snapshot(snapshot)

    assert "visual-20" not in rendered
    assert "motion-8" not in rendered
    assert "sfx-placement-15" not in rendered
    assert "card-12" not in rendered
    assert "cue-40" not in rendered
    assert "word-150" not in rendered
    assert "text-0" not in rendered


def test_context_and_visual_details_are_allowlisted_and_url_safe() -> None:
    snapshot = _negotiated_snapshot()
    snapshot["source_assets"][0]["context"].update(
        {
            "description": "A long description with https://evil.example/secret and a useful tail",
            "nested": {"secret": "nested instruction"},
            "source": "https://evil.example/source",
        }
    )
    snapshot["visual_blocks"][0]["details"] = {
        "asset_id": "asset-safe",
        "media_kind": "image",
        "display_mode": "overlay",
        "x_frac": 0.4,
        "transform": {"fit_mode": "cover", "focal_x": 0.5, "unknown": "drop me"},
        "src_gcs_path": "gs://bucket/private.mp4",
    }
    snapshot["visual_blocks"][1]["details"] = {
        "shots": [
            {
                "id": "shot-safe",
                "asset_id": "asset-safe",
                "kind": "video",
                "start_offset_s": 0,
                "duration_s": 1,
                "trim_start_s": 0,
                "crop": {"x_frac": 0.5, "y_frac": 0.5, "scale": 1, "secret": "drop me"},
                "motion": "zoom_in",
                "sync_anchor": {"type": "keyword", "time_s": 0.2, "label": "reveal"},
                "context": {"subject": "shot subject", "source": "https://evil.example/shot"},
                "src_gcs_path": "gs://bucket/shot.mp4",
            }
        ]
    }

    rendered = _format_snapshot(snapshot)

    assert "useful tail" in rendered
    assert "evil.example" not in rendered
    assert "nested instruction" not in rendered
    assert "private.mp4" not in rendered
    assert "shot-safe" in rendered
    assert "drop me" not in rendered
    assert "secret" not in rendered
    assert "sync_anchor={type='keyword', time_s=0.200, label='reveal'}" in rendered


def test_focus_is_omitted_when_selection_is_stale() -> None:
    snapshot = _negotiated_snapshot()
    snapshot["editor_focus"]["selected"] = {
        "kind": "caption",
        "id": "missing-caption",
        "index": 999,
    }

    rendered = _format_snapshot(snapshot)

    assert "playhead_s=1.250 selected=(none)" in rendered


def test_prompt_and_parser_support_arbitrary_context_grounded_selection() -> None:
    snapshot = {
        "component_context_version": 1,
        "allowed_op_families": ["text"],
        "text_bars": [
            {
                "id": "caption-0",
                "role": "narrated_caption",
                "text": "same words",
                "start_s": 0,
                "end_s": 1,
                "timing_locked": True,
                "context": {"semantic_role": "spoken transcript"},
            },
            {
                "id": "badge-0",
                "role": "overlay_text",
                "text": "same words",
                "start_s": 0,
                "end_s": 1,
                "context": {"semantic_role": "product_price_badge"},
            },
        ],
    }
    agent = EditCopilotAgent(MagicMock())
    prompt = agent.render_prompt(
        EditCopilotInput(utterance="keep the product price badge longer", variant_snapshot=snapshot)
    )
    parsed = agent.parse(
        json.dumps(
            {
                "intent": "edit",
                "confidence": 0.9,
                "reply": "I extended the product price badge.",
                "ops": [{"op": "set_text_timing", "bar_index": 1, "end_s": 2}],
            }
        ),
        EditCopilotInput(
            utterance="keep the product price badge longer", variant_snapshot=snapshot
        ),
    )

    assert "component_context_version=1" in prompt
    assert "arbitrary semantic_role labels" in prompt
    assert "Opaque IDs do not establish image meaning" in prompt
    assert "allowed_op_families remains the" in prompt
    assert parsed.ops == [{"op": "set_text_timing", "bar_index": 1, "end_s": 2.0}]


def test_complete_text_and_currency_survive_prompt_building() -> None:
    text = "Context " * 55 + "Sale price $4 {today}"
    snapshot = {
        "component_context_version": 1,
        "allowed_op_families": ["text"],
        "text_bars": [
            {
                "id": "sale",
                "text": text,
                "start_s": 0,
                "end_s": 2,
                "text_case": "upper",
                "stroke_width": 2,
            }
        ],
        "captions": {"cues": [{"index": 0, "id": "cue", "text": text}], "meta": {}},
    }
    prompt = EditCopilotAgent(MagicMock()).render_prompt(
        EditCopilotInput(
            utterance="Change the sale price to $5",
            variant_snapshot=snapshot,
        )
    )
    assert prompt.count(text) == 2
    assert "text_case=upper" in prompt
    assert "stroke_width=2" in prompt
    assert "Change the sale price to $5" in prompt


def test_music_focus_accepts_the_actual_background_lane_identity() -> None:
    rendered = _format_snapshot(
        {
            "component_context_version": 1,
            "music": {"current_track_id": "track-1", "candidates": []},
            "editor_focus": {"selected": {"kind": "music", "id": "background"}},
        }
    )
    assert "kind='music'" in rendered
    assert "id='background'" in rendered


def test_readonly_context_does_not_advertise_all_operations():
    rendered = _format_snapshot(
        {
            "component_context_version": 1,
            "allowed_op_families": [],
            "asset_context_status": ["ready"],
        }
    )
    assert "(none; read-only inspection)" in rendered
    assert "all v1 ops" not in rendered


@pytest.mark.parametrize("negotiated", [False, True], ids=["legacy", "negotiated"])
@pytest.mark.parametrize(
    "families",
    [{}, {"allowed_op_families": None}, {"allowed_op_families": []}],
    ids=["missing", "null", "empty"],
)
def test_empty_families_parser_contract(negotiated: bool, families: dict) -> None:
    snapshot = {
        **families,
        "total_duration_s": 5,
        "text_bars": [{"text": "Price", "start_s": 0, "end_s": 1}],
    }
    if negotiated:
        snapshot["component_context_version"] = 1
    operation = {"op": "set_text_timing", "bar_index": 0, "end_s": 2.0}
    parsed = EditCopilotAgent(MagicMock()).parse(
        json.dumps(
            {
                "intent": "edit",
                "confidence": 0.9,
                "reply": "I extended the price text.",
                "ops": [operation],
            }
        ),
        EditCopilotInput(utterance="Keep the price text longer", variant_snapshot=snapshot),
    )
    assert parsed.ops == ([] if negotiated else [operation])


@pytest.mark.parametrize("negotiated", [False, True], ids=["legacy", "negotiated"])
def test_music_candidates_follow_component_context_retention(negotiated: bool) -> None:
    snapshot = {
        "music": {
            "candidates": [
                {"id": f"track-{index}", "title": f"Song {index}"} for index in range(21)
            ]
        }
    }
    if negotiated:
        snapshot["component_context_version"] = 1
    rendered = _format_snapshot(snapshot)
    assert "id='track-19'" in rendered
    assert ("id='track-20'" in rendered) is negotiated
