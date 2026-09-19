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


def test_consecutive_drafts_preserve_prior_text_and_other_sections() -> None:
    from app.services.kria_editor_ops import merge_editor_draft, project_editor_draft

    variant = _variant()
    first = compile_editor_ops(
        _job(variant),
        variant,
        [
            {"op": "edit_text", "bar_index": 0, "text": "Fresh hook"},
        ],
    ).payload.model_dump(mode="json", exclude_none=True)
    projected = project_editor_draft(variant, first)
    assert projected["text_elements"][0]["text"] == "Fresh hook"
    second = compile_editor_ops(
        _job(variant),
        projected,
        [
            {"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#FF0000"}},
        ],
    ).payload.model_dump(mode="json", exclude_none=True)
    merged = merge_editor_draft(first, second)
    assert merged["text_elements"][0]["text"] == "Fresh hook"
    assert merged["text_elements"][0]["color"] == "#FF0000"
    assert variant["text_elements"][0]["text"] == "Old hook"
    assert merge_editor_draft({"remove_music": True}, {"remove_music": False})["remove_music"]


def _media_block(block_id="media-1"):
    return {
        "id": block_id,
        "kind": "media",
        "asset_id": "uploaded-asset",
        "src_gcs_path": "users/u/private-image.jpg",
        "media_kind": "image",
        "start_s": 0.0,
        "end_s": 2.0,
        "origin": "user",
    }


def _enable_visuals(monkeypatch, variant):
    variant["base_video_path"] = "users/u/base.mp4"
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda *_: {"visual_blocks": True},
    )


def test_visual_media_snapshot_exposes_only_safe_removable_media(monkeypatch):
    variant = _variant()
    _enable_visuals(monkeypatch, variant)
    variant["visual_blocks"] = [
        _media_block(),
        {"id": "card", "kind": "text_card"},
        {**_media_block("ai-media"), "origin": "ai"},
    ]
    variant["media_overlays"] = [{"id": "legacy", "src_gcs_path": "users/u/old.jpg"}]
    snapshot = build_editor_snapshot(_job(variant), variant)
    assert "visual_media" in snapshot["allowed_op_families"]
    assert snapshot["visual_media"] == [
        {
            "id": "media-1",
            "media_kind": "image",
            "start_s": 0.0,
            "end_s": 2.0,
            "origin": "user",
        }
    ]
    assert "users/" not in json.dumps(snapshot)
    assert "legacy" not in json.dumps(snapshot)


def test_remove_visual_media_preserves_every_unrelated_lane(monkeypatch):
    from app.agents._schemas.visual_block import validate_visual_blocks
    from app.services.kria_editor_ops import project_editor_draft

    variant = _variant()
    _enable_visuals(monkeypatch, variant)
    card = {
        "id": "card",
        "kind": "text_card",
        "start_s": 0.0,
        "end_s": 2.0,
        "background": {"type": "solid", "color": "#000000"},
    }
    variant["visual_blocks"] = [_media_block(), _media_block("keep"), card]
    variant["media_overlays"] = [{"id": "legacy"}]
    variant["sound_effects"] = [{"id": "sound"}]
    variant["camera_effects"] = [{"id": "camera"}]
    before = json.dumps(variant, sort_keys=True)
    payload = compile_editor_ops(
        _job(variant),
        variant,
        [
            {"op": "remove_visual_media", "target_ids": ["media-1"]},
        ],
    ).payload
    raw = payload.model_dump(mode="json", exclude_none=True)
    assert [row["id"] for row in raw["visual_blocks"]] == ["keep", "card"]
    assert payload.text_elements is payload.caption_cues is payload.timeline_slots is None
    assert payload.media_overlays is payload.sound_effects is payload.mix is None
    assert payload.remove_music is False
    validate_visual_blocks(raw["visual_blocks"], duration_s=4.0)
    projected = project_editor_draft(variant, raw)
    for lane in (
        "text_elements",
        "caption_cues",
        "ai_timeline",
        "media_overlays",
        "sound_effects",
        "camera_effects",
        "music_track_id",
        "mix",
    ):
        assert projected[lane] == variant[lane]
    assert json.dumps(variant, sort_keys=True) == before


@pytest.mark.parametrize("targets", [[], ["missing"], ["card"], ["media-1", "media-1"], [True]])
def test_visual_removal_rejects_invalid_targets_atomically(monkeypatch, targets):
    variant = _variant()
    _enable_visuals(monkeypatch, variant)
    variant["visual_blocks"] = [_media_block(), {"id": "card", "kind": "text_card"}]
    before = json.dumps(variant, sort_keys=True)
    with pytest.raises(KriaEditorOpError):
        compile_editor_ops(
            _job(variant), variant, [{"op": "remove_visual_media", "target_ids": targets}]
        )
    assert json.dumps(variant, sort_keys=True) == before


@pytest.mark.parametrize("reason", ["no_base", "lyrics", "linked_text", "disabled", "ai_origin"])
def test_visual_removal_rejects_uneditable_or_text_linked_media(monkeypatch, reason):
    variant = _variant()
    _enable_visuals(monkeypatch, variant)
    variant["visual_blocks"] = [_media_block()]
    if reason == "no_base":
        variant.pop("base_video_path")
    elif reason == "lyrics":
        variant["text_mode"] = "lyrics"
    elif reason == "linked_text":
        variant["text_elements"][0]["visual_block_id"] = "media-1"
    elif reason == "ai_origin":
        variant["visual_blocks"][0]["origin"] = "ai"
    else:
        monkeypatch.setattr("app.services.kria_editor_ops._editor_capabilities", lambda *_: {})
    assert (
        "visual_media" not in build_editor_snapshot(_job(variant), variant)["allowed_op_families"]
    )
    with pytest.raises(KriaEditorOpError, match="no longer removable"):
        compile_editor_ops(
            _job(variant), variant, [{"op": "remove_visual_media", "target_ids": ["media-1"]}]
        )


def test_guided_visual_removal_uses_revision_lane_and_fences_commit(monkeypatch):
    from app.services.kria_editor_ops import project_editor_draft

    variant = _variant()
    _enable_visuals(monkeypatch, variant)
    variant["visual_blocks"] = [_media_block("stale")]
    variant["guided_edit_revision"] = {
        "revision_number": 7,
        "visual_blocks": [_media_block()],
        "text_elements": [],
    }
    monkeypatch.setattr(
        "app.services.kria_editor_ops._guided_v2_revision",
        lambda _job, row: row["guided_edit_revision"],
    )
    payload = compile_editor_ops(
        _job(variant),
        variant,
        [
            {
                "op": "remove_visual_media",
                "target_ids": ["media-1"],
            }
        ],
    ).payload
    assert payload.guided_revision_number == 7
    assert payload.guided_revision is None
    assert payload.visual_blocks == []
    projected = project_editor_draft(variant, payload.model_dump(mode="json", exclude_none=True))
    assert projected["guided_edit_revision"]["visual_blocks"] == []
    assert (
        "visual_media"
        not in build_editor_snapshot(_job(projected), projected)["allowed_op_families"]
    )


def test_real_guided_save_removes_media_preserving_revision_and_narration(monkeypatch):
    import copy

    import app.routes.generative_jobs as gj
    from app.config import settings
    from tests.routes.test_editor_commit import _arm, _narrated_guided_job

    _arm(monkeypatch)
    monkeypatch.setattr(settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    job = _narrated_guided_job()
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    revision["visual_blocks"] = [_media_block()]
    revision["state_hash"] = ""
    variant["guided_edit_revision"] = revision
    variant["visual_blocks"] = [_media_block()]
    before = copy.deepcopy(gj._guided_v2_revision(job, variant))
    assert "visual_media" in build_editor_snapshot(job, variant)["allowed_op_families"]
    payload = compile_editor_ops(
        job,
        variant,
        [
            {
                "op": "remove_visual_media",
                "target_ids": ["media-1"],
            }
        ],
    ).payload
    result = gj.prepare_editor_commit(job, "song_text", payload)
    saved = job.assembly_plan["variants"][0]["guided_edit_revision"]
    assert result["sections"]["visual_blocks"] is True
    assert saved["visual_blocks"] == []
    assert saved["revision_number"] == before["revision_number"] + 1
    for lane in (
        "text_elements",
        "segments",
        "audio",
        "sources",
        "sound_effects",
        "media_overlays",
    ):
        assert saved[lane] == before[lane]


def _variant_with_bars(*texts: str) -> dict:
    variant = _variant()
    variant["text_elements"] = [
        {**_variant()["text_elements"][0], "id": f"text-{index}", "text": text}
        for index, text in enumerate(texts)
    ]
    return variant


def test_remove_text_bundle_addresses_the_snapshot_bars_not_the_shrinking_list() -> None:
    # 2026-09-19: "remove the texts that aren't the titles" on a six-bar guided
    # story emitted remove_text 1..4; popping in place deleted bars 1, 3, 5 and
    # then rejected bar 4 as out of range, so nothing was saved.
    variant = _variant_with_bars(
        "Title", "Thought A", "Thought B", "Thought C", "Thought D", "Closing"
    )
    assert [row["id"] for row in variant["text_elements"]] == [f"text-{i}" for i in range(6)]

    compiled = compile_editor_ops(
        _job(variant),
        variant,
        [{"op": "remove_text", "bar_index": index} for index in (1, 2, 3, 4)],
    )

    saved = compiled.payload.model_dump(mode="json", exclude_none=True)["text_elements"]
    assert [row["id"] for row in saved] == ["text-0", "text-5"]
    assert [row["text"] for row in variant["text_elements"]][1:5] == [
        "Thought A",
        "Thought B",
        "Thought C",
        "Thought D",
    ]


def test_text_edits_after_a_removal_still_target_snapshot_indexes() -> None:
    variant = _variant_with_bars("Title", "Thought A", "Closing")

    compiled = compile_editor_ops(
        _job(variant),
        variant,
        [
            {"op": "remove_text", "bar_index": 1},
            {"op": "edit_text", "bar_index": 2, "text": "The end"},
            {"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#FF0000"}},
        ],
    )

    saved = compiled.payload.model_dump(mode="json", exclude_none=True)["text_elements"]
    assert [(row["id"], row["text"]) for row in saved] == [
        ("text-0", "Title"),
        ("text-2", "The end"),
    ]
    assert saved[0]["color"] == "#FF0000"


@pytest.mark.parametrize(
    "second",
    [
        {"op": "remove_text", "bar_index": 1},
        {"op": "edit_text", "bar_index": 1, "text": "ghost"},
        {"op": "set_text_timing", "bar_index": 1, "start_s": 0.5, "end_s": 1.5},
    ],
)
def test_text_ops_on_a_bar_removed_earlier_in_the_bundle_reject(second: dict) -> None:
    variant = _variant_with_bars("Title", "Thought A", "Closing")
    with pytest.raises(KriaEditorOpError, match="Text changed"):
        compile_editor_ops(
            _job(variant),
            variant,
            [{"op": "remove_text", "bar_index": 1}, second],
        )


def test_snapshot_text_bars_expose_centre_fractions(monkeypatch) -> None:
    variant = _variant()
    variant["text_elements"][0].update({"position": "custom", "x_frac": 0.3, "y_frac": 0.12})
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda _job, _variant: {"text_elements": True},
    )

    snapshot = build_editor_snapshot(_job(variant), variant)

    bar = snapshot["text_bars"][0]
    assert (bar["position"], bar["x_frac"], bar["y_frac"]) == ("custom", 0.3, 0.12)


def test_rotation_round_trips_through_style_patch_and_snapshot(monkeypatch) -> None:
    variant = _variant()
    variant["text_elements"][0]["rotation_deg"] = -12.0
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda _job, _variant: {"text_elements": True},
    )
    assert build_editor_snapshot(_job(variant), variant)["text_bars"][0]["rotation_deg"] == -12.0

    compiled = compile_editor_ops(
        _job(variant),
        variant,
        [{"op": "patch_text_style", "bar_index": 0, "patch": {"rotation_deg": 8}}],
    )
    saved = compiled.payload.model_dump(mode="json", exclude_none=True)["text_elements"]
    assert saved[0]["rotation_deg"] == 8


def _appearance_variant() -> dict:
    variant = _variant_with_bars("Emir Olympics", "post match pub")
    for row in variant["text_elements"]:
        row.update({"stroke_width": 2.0, "shadow_enabled": True})
    return variant


def test_snapshot_advertises_the_text_appearance_inventory_when_enabled(monkeypatch) -> None:
    # 2026-09-19 (job d9a965b0): "remove all shadow and outline" was rejected
    # because the server-side chat snapshot never carried the inventory the
    # web drawer builds client-side; the atomic rule then dropped the font,
    # size and placement ops alongside it.
    variant = _appearance_variant()
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda _job, _variant: {"text_elements": True},
    )
    monkeypatch.setattr("app.config.settings.text_appearance_enabled", True)

    snapshot = build_editor_snapshot(_job(variant), variant)

    assert snapshot["text_appearance_version"] == 1
    targets = snapshot["text_appearance"]["targets"]
    assert [t["id"] for t in targets] == ["text-0", "text-1"]
    assert targets[0]["kind"] == "text"
    assert targets[0]["supported_fields"] == ["stroke_width", "shadow_enabled"]
    assert targets[0]["values"] == {"stroke_width": 2.0, "shadow_enabled": True}
    assert isinstance(targets[0]["identity"], str) and targets[0]["identity"]

    monkeypatch.setattr("app.config.settings.text_appearance_enabled", False)
    assert "text_appearance" not in build_editor_snapshot(_job(variant), variant)


def test_prod_appearance_bundle_parses_and_compiles_end_to_end(monkeypatch) -> None:
    from app.agents._runtime import ModelClient
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput

    variant = _appearance_variant()
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda _job, _variant: {"text_elements": True},
    )
    monkeypatch.setattr("app.config.settings.text_appearance_enabled", True)
    snapshot = build_editor_snapshot(_job(variant), variant)
    raw = json.dumps(
        {
            "intent": "edit",
            "ops": [
                {
                    "op": "patch_text_appearance",
                    "selector": {"scope": "editable_text", "quantifier": "all"},
                    "patch": {"stroke_width": 0, "shadow_enabled": False},
                    "text_appearance_version": 1,
                },
                {
                    "op": "patch_text_style",
                    "bar_index": 0,
                    "patch": {
                        "font_family": "Inter",
                        "size_px": 64.0,
                        "alignment": "left",
                        "position": "custom",
                        "x_frac": 0.3,
                        "y_frac": 0.12,
                    },
                },
                {
                    "op": "patch_text_style",
                    "bar_index": 1,
                    "patch": {
                        "font_family": "Inter",
                        "size_px": 64.0,
                        "alignment": "left",
                        "position": "custom",
                        "x_frac": 0.3,
                        "y_frac": 0.12,
                    },
                },
            ],
            "confidence": 0.95,
            "reply": "Done.",
            "suggestions": [],
            "needs_clarification": False,
        }
    )
    output = EditCopilotAgent(ModelClient()).parse(
        raw,
        EditCopilotInput(
            utterance=(
                "Change the texts to inter font, remove all shadow and outline. "
                "Place the texts near top left. Lower the font size"
            ),
            prior_turns=[],
            variant_snapshot=snapshot,
        ),
    )
    assert output.outcome == "proposed"
    assert [op["op"] for op in output.ops] == [
        "patch_text_appearance",
        "patch_text_style",
        "patch_text_style",
    ]

    compiled = compile_editor_ops(_job(variant), variant, output.ops)
    saved = compiled.payload.model_dump(mode="json", exclude_none=True)["text_elements"]
    for row in saved:
        assert (row["stroke_width"], row["shadow_enabled"]) == (0, False)
        assert (row["font_family"], row["size_px"], row["position"]) == ("Inter", 64.0, "custom")
        assert (row["x_frac"], row["y_frac"], row["alignment"]) == (0.3, 0.12, "left")


def test_patch_text_appearance_rejects_a_target_removed_earlier_in_the_bundle() -> None:
    variant = _appearance_variant()
    with pytest.raises(KriaEditorOpError, match="Text changed"):
        compile_editor_ops(
            _job(variant),
            variant,
            [
                {"op": "remove_text", "bar_index": 1},
                {
                    "op": "patch_text_appearance",
                    "patch": {"shadow_enabled": False},
                    "target_ids": ["text-0", "text-1"],
                },
            ],
        )
