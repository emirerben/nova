from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services.kria_editor_ops import (
    KriaEditorOpError,
    build_editor_snapshot,
    compile_editor_ops,
)


def _variant() -> dict:
    return {
        "variant_id": "original_text",
        "render_status": "ready",
        "render_generation_id": "gen-1",
        "resolved_archetype": "montage",
        "mix": 0.5,
        "music_track_id": "00000000-0000-0000-0000-000000000001",
        "text_elements": [
            {
                "id": "text-1",
                "text": "Old hook",
                "start_s": 0.0,
                "end_s": 2.0,
                "font_family": "Playfair Display",
                "size_px": 72,
                "color": "#FFFFFF",
                "effect": "static",
                "alignment": "center",
                "position": "middle",
            }
        ],
        "caption_cues": [{"id": "cue-1", "text": "Kriya", "start_s": 0.0, "end_s": 1.0}],
        "ai_timeline": {
            "beat_grid": [],
            "slots": [
                {
                    "slot_id": "slot-1",
                    "clip_index": 0,
                    "source_gcs_path": "users/u/a.mp4",
                    "source_duration_s": 8.0,
                    "in_s": 0.0,
                    "duration_beats": None,
                    "duration_s": 2.0,
                    "removed": False,
                    "transition_after": "cut",
                },
                {
                    "slot_id": "slot-2",
                    "clip_index": 1,
                    "source_gcs_path": "users/u/b.jpg",
                    "source_duration_s": 3.0,
                    "in_s": 0.0,
                    "duration_beats": None,
                    "duration_s": 2.0,
                    "removed": False,
                    "transition_after": "cut",
                },
            ],
        },
    }


def _job(variant: dict) -> SimpleNamespace:
    return SimpleNamespace(
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": ["users/u/a.mp4", "users/u/b.jpg", "users/u/c.png"]},
    )


def test_snapshot_is_path_free_and_bounded_to_portable_families(monkeypatch) -> None:
    variant = _variant()
    job = _job(variant)
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda _job, _variant: {
            "text_elements": True,
            "timeline": True,
            "clips": {"transitions": {"editable": True}},
            "music_operations": {"remove": True},
            "mix": True,
        },
    )

    snapshot = build_editor_snapshot(job, variant)

    encoded = json.dumps(snapshot)
    assert "users/" not in encoded
    assert "signed_url" not in encoded
    assert snapshot["allowed_op_families"] == [
        "caption",
        "clip",
        "music",
        "text",
        "title",
        "transition",
    ]


def test_compiles_cross_lane_bundle_into_one_atomic_editor_commit() -> None:
    variant = _variant()
    compiled = compile_editor_ops(
        _job(variant),
        variant,
        [
            {"op": "edit_text", "bar_index": 0, "text": "Fresh matcha, finally"},
            {"op": "reorder_clip", "from_index": 1, "to_index": 0},
            {
                "op": "set_transition",
                "boundary_index": 0,
                "transition": "crossfade",
                "duration_s": 0.25,
            },
            {"op": "set_mix", "music_level": 0.35},
        ],
    )

    assert compiled.payload.base_generation == "gen-1"
    assert compiled.payload.text_elements[0]["text"] == "Fresh matcha, finally"
    assert [slot.slot_id for slot in compiled.payload.timeline_slots] == ["slot-2", "slot-1"]
    assert compiled.payload.timeline_slots[0].transition_after == "crossfade"
    assert compiled.payload.mix.music_level == 0.35
    assert compiled.changes == ["Edit text", "Reorder clip", "Set transition"]


def test_compiles_mixed_media_bulk_operations_without_paths() -> None:
    variant = _variant()
    compiled = compile_editor_ops(
        _job(variant),
        variant,
        [
            {
                "op": "add_unused_sources",
                "selector": {
                    "scope": "unused_sources",
                    "media_kind": "image",
                    "quantifier": "all",
                },
            },
            {
                "op": "set_media_duration",
                "selector": {
                    "scope": "timeline",
                    "media_kind": "image",
                    "quantifier": "all",
                },
                "duration_s": 1.5,
            },
            {
                "op": "stack_images",
                "selector": {
                    "scope": "timeline",
                    "media_kind": "image",
                    "quantifier": "all",
                },
            },
        ],
    )

    slots = compiled.payload.timeline_slots
    assert [slot.clip_index for slot in slots] == [0, 1, 2]
    assert [slot.duration_s for slot in slots if slot.clip_index in {1, 2}] == [1.5, 1.5]
    assert "users/" not in compiled.payload.model_dump_json()


def test_compiles_caption_text_timing_style_and_music_removal() -> None:
    variant = _variant()
    compiled = compile_editor_ops(
        _job(variant),
        variant,
        [
            {"op": "replace_caption_text", "find": "Kriya", "replace": "Kria"},
            {"op": "set_caption_timing", "cue_index": 0, "end_s": 1.2},
            {"op": "set_caption_meta", "patch": {"style": "word", "enabled": True}},
            {"op": "remove_music"},
        ],
    )

    assert compiled.payload.caption_cues == [
        {"id": "cue-1", "text": "Kria", "start_s": 0.0, "end_s": 1.2}
    ]
    assert compiled.payload.caption_meta.style == "word"
    assert compiled.payload.remove_music is True


def test_unknown_operation_rejects_without_mutating_authoritative_variant() -> None:
    variant = _variant()
    original = json.dumps(variant, sort_keys=True)

    with pytest.raises(KriaEditorOpError, match="not portable"):
        compile_editor_ops(_job(variant), variant, [{"op": "open_tool", "tool": "styles"}])

    assert json.dumps(variant, sort_keys=True) == original


def test_reviewed_speech_cut_is_a_separate_revision_fenced_draft() -> None:
    from app.pipeline.speech_cut_state import cut_revision

    variant = _variant()
    variant["resolved_archetype"] = "talking_head"
    variant["speech_cut_revision"] = "cut-rev-4"
    variant["speech_cut_candidates"] = [
        {
            "candidate_id": "retake-1",
            "source": "retake_review",
            "status": "pending",
            "start_s": 1.0,
            "end_s": 1.7,
        }
    ]

    compiled = compile_editor_ops(
        _job(variant),
        variant,
        [{"op": "apply_speech_cut_candidate", "candidate_id": "retake-1"}],
    )

    assert compiled.payload == {
        "operation": "speech_cut",
        "candidate_id": "retake-1",
        "expected_revision": cut_revision(variant),
    }
    assert compiled.changes == ["Apply reviewed speech cut"]

    with pytest.raises(KriaEditorOpError, match="rendered on its own"):
        compile_editor_ops(
            _job(variant),
            variant,
            [
                {"op": "apply_speech_cut_candidate", "candidate_id": "retake-1"},
                {"op": "set_mix", "music_level": 0.2},
            ],
        )


@pytest.mark.parametrize(
    ("ops", "message"),
    [
        ([], "No safe draft change"),
        ([{"op": "set_title", "title": "New title"}] * 9, "at most eight"),
        ([{"op": "edit_text", "bar_index": True, "text": "New hook"}], "Text changed"),
        (
            [{"op": "patch_text_style", "bar_index": 0, "patch": {"unsupported": 1}}],
            "No portable text style fields",
        ),
        ([{"op": "trim_output_start", "start_s": 0}], "has no effect"),
        ([{"op": "split_clip", "slot_index": 0, "at_s": 0}], "outside the clip"),
        (
            [{"op": "set_transition", "boundary_index": 1, "transition": "cut"}],
            "Transition changed",
        ),
        (
            [{"op": "replace_caption_text", "find": "missing", "replace": "Kria"}],
            'No captions contain "missing"',
        ),
        (
            [{"op": "apply_speech_cut_candidate", "candidate_id": "missing"}],
            "no longer available",
        ),
    ],
)
def test_editor_compiler_rejects_unsafe_or_stale_boundaries(ops: list[dict], message: str) -> None:
    variant = _variant()

    with pytest.raises(KriaEditorOpError, match=message):
        compile_editor_ops(_job(variant), variant, ops)


def test_editor_compiler_materializes_remaining_portable_sections() -> None:
    variant = _variant()

    compiled = compile_editor_ops(
        _job(variant),
        variant,
        [
            {"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#00FF00"}},
            {"op": "set_text_timing", "bar_index": 0, "start_s": 0.2, "end_s": 1.8},
            {"op": "set_clip_duration", "slot_index": 0, "duration_s": 1.5},
            {"op": "set_clip_in", "slot_index": 0, "in_s": 0.4},
            {"op": "set_look_preset", "slot_index": 0, "look_preset": "none"},
            {"op": "edit_caption", "cue_index": 0, "text": "Kria"},
            {"op": "set_caption_emphasis", "cue_index": 0, "emphasis": True},
            {"op": "set_title", "title": "Matcha launch day"},
        ],
    )

    assert compiled.payload.text_elements[0] == {
        **variant["text_elements"][0],
        "color": "#00FF00",
        "start_s": 0.2,
        "end_s": 1.8,
    }
    assert compiled.payload.timeline_slots[0].duration_s == 1.5
    assert compiled.payload.timeline_slots[0].in_s == 0.4
    assert compiled.payload.timeline_slots[0].look_preset == "none"
    assert compiled.payload.caption_cues[0]["text"] == "Kria"
    assert compiled.payload.caption_cues[0]["smart_emphasis"] is True
    assert compiled.payload.title == "Matcha launch day"
