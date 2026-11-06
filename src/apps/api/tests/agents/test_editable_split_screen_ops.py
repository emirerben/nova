"""Offline parser/application/commit round trips for independently editable panels.

Failure cases: lost membership or appearance; summed panel duration; wrong source;
peer timing changes; stale manual state; invalid membership; unavailable capability.
"""

from copy import deepcopy

import pytest

from app.agents.edit_copilot import _ParseState, parse_editor_operation
from app.services import kria_editor_ops as editor
from tests.services.test_kria_editor_ops import _job, _variant


@pytest.fixture
def document(monkeypatch):
    variant = _variant()
    variant["render_destination"] = "device"
    variant["editor_timeline_mode"] = "authored"
    variant["user_timeline"] = deepcopy(variant.pop("ai_timeline"))
    variant["user_timeline"]["slots"][0].update(
        muted=True,
        layout="fullscreen",
        playback_rate=1.0,
        source_crop={"x": 0.1, "y": 0.2, "width": 0.6, "height": 0.5},
    )
    monkeypatch.setattr(
        editor,
        "_editor_capabilities",
        lambda *args: {
            "timeline": True,
            "clips": {"source_crop": True},
        },
    )
    monkeypatch.setattr(editor, "split_screen_available", lambda *args: True)
    return _job(variant), variant


def apply(job, variant, raw):
    snapshot = editor.build_editor_snapshot(job, variant)
    snapshot["editor_ops_version"] = 2
    parsed = parse_editor_operation(raw, snapshot, _ParseState(0.99))
    assert parsed is not None, raw
    draft = editor.compile_editor_ops(job, variant, [parsed])
    payload = draft.payload.model_dump(mode="json", exclude_none=True)
    return editor.project_editor_draft(variant, payload, job), payload


def test_ai_manual_ai_preserves_sources_appearance_and_unrelated_edit(document):
    job, variant = document
    original = deepcopy(variant["user_timeline"]["slots"])
    variant, payload = apply(
        job,
        variant,
        {
            "op": "create_split_screen",
            "slot_indexes": [0, 1],
            "panel_count": 2,
            "duration_s": 4,
        },
    )
    section = payload["split_screen_sections"][0]
    assert [r["clip_index"] for r in payload["timeline_slots"]] == [0, 1]
    assert [r["split_panel_index"] for r in payload["timeline_slots"]] == [0, 1]
    for key in ("muted", "layout", "source_crop", "playback_rate"):
        assert payload["timeline_slots"][0][key] == original[0][key]
    variant["user_timeline"]["slots"][0]["in_s"] = 1.25
    variant["text_elements"][0]["text"] = "Manual title"
    variant, payload = apply(
        job,
        variant,
        {
            "op": "update_split_screen",
            "section_id": section["id"],
            "gutter": 0.03,
            "panel_weights": [2, 1],
            "background_color": "#123456",
        },
    )
    snapshot = editor.build_editor_snapshot(job, variant)
    assert snapshot["slots"][0]["in_s"] == 1.25
    assert snapshot["slots"][0]["split_section_id"] == section["id"]
    assert snapshot["split_screen_sections"][0]["panel_weights"] == [2, 1]
    assert variant["text_elements"][0]["text"] == "Manual title"
    assert snapshot["total_duration_s"] == 4


def test_split_member_keeps_peer_clock_and_properties(document):
    job, variant = document
    variant, _ = apply(
        job,
        variant,
        {
            "op": "create_split_screen",
            "slot_indexes": [0, 1],
            "panel_count": 2,
        },
    )
    peer = deepcopy(variant["user_timeline"]["slots"][1])
    variant, payload = apply(job, variant, {"op": "split_clip", "slot_index": 0, "at_s": 1})
    rows = variant["user_timeline"]["slots"]
    assert rows[1]["split_offset_s"] == 1
    assert rows[1]["in_s"] == 1
    assert rows[2] == peer
    assert rows[0]["muted"] is rows[1]["muted"] is True
    assert payload["split_screen_sections"][0]["duration_s"] == 2


def test_invalid_panel_and_capability_refused(document, monkeypatch):
    job, variant = document
    snapshot = editor.build_editor_snapshot(job, variant)
    snapshot["editor_ops_version"] = 2
    for count in (1, 4, True):
        assert (
            parse_editor_operation(
                {
                    "op": "create_split_screen",
                    "slot_indexes": [0],
                    "panel_count": count,
                },
                snapshot,
                _ParseState(0.99),
            )
            is None
        )
    monkeypatch.setattr(editor, "split_screen_available", lambda *args: False)
    snapshot = editor.build_editor_snapshot(job, variant)
    snapshot["editor_ops_version"] = 2
    assert (
        parse_editor_operation(
            {
                "op": "create_split_screen",
                "slot_indexes": [0],
                "panel_count": 2,
            },
            snapshot,
            _ParseState(0.99),
        )
        is None
    )


def test_swap_offset_and_unstack_round_trip(document):
    job, variant = document
    variant, payload = apply(
        job,
        variant,
        {
            "op": "create_split_screen",
            "slot_indexes": [0, 1],
            "panel_count": 3,
            "duration_s": 5,
        },
    )
    section_id = payload["split_screen_sections"][0]["id"]
    variant, _ = apply(
        job,
        variant,
        {
            "op": "assign_split_panel",
            "section_id": section_id,
            "slot_index": 1,
            "panel_index": 1,
            "offset_s": 1,
        },
    )
    variant, _ = apply(
        job,
        variant,
        {"op": "swap_split_panels", "section_id": section_id, "first_panel": 0, "second_panel": 1},
    )
    rows = variant["user_timeline"]["slots"]
    source_b = next(r for r in rows if r["clip_index"] == 1)
    assert source_b["split_panel_index"] == 0
    assert source_b["split_offset_s"] == 1
    variant, payload = apply(
        job, variant, {"op": "remove_split_screen", "section_id": section_id, "keep_panel": 0}
    )
    assert payload["split_screen_sections"] == []
    assert [r["clip_index"] for r in variant["user_timeline"]["slots"]] == [1]
    assert not variant["user_timeline"]["slots"][0].get("split_section_id")


def test_bad_overlap_refuses_without_mutation(document):
    job, variant = document
    variant, payload = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0, 1], "panel_count": 2}
    )
    original = deepcopy(variant)
    for operation in [
        {
            "op": "assign_split_panel",
            "section_id": payload["split_screen_sections"][0]["id"],
            "slot_index": 1,
            "panel_index": 0,
        },
    ]:
        with pytest.raises(editor.KriaEditorOpError):
            editor.compile_editor_ops(job, variant, [operation])
        assert variant == original


def test_bounded_section_retains_fullscreen_neighbors(document):
    job, variant = document
    variant["user_timeline"]["slots"][0]["duration_s"] = 6
    variant, payload = apply(
        job,
        variant,
        {
            "op": "create_split_screen",
            "slot_indexes": [0],
            "panel_count": 2,
            "start_s": 1,
            "end_s": 4,
        },
    )
    rows = payload["timeline_slots"]
    assert [r["duration_s"] for r in rows] == [1, 3, 3, 2, 2]
    assert [r["in_s"] for r in rows[:4]] == [0, 1, 1, 4]
    assert len({r["slot_id"] for r in rows}) == len(rows)
    assert payload["split_screen_sections"][0]["duration_s"] == 3
    assert editor.build_editor_snapshot(job, variant)["total_duration_s"] == 8


def test_full_length_duplicates_sequence_with_independent_clocks(document):
    job, variant = document
    variant, payload = apply(
        job,
        variant,
        {
            "op": "create_split_screen",
            "slot_indexes": [0, 1],
            "panel_count": 2,
            "full_length": True,
        },
    )
    rows = payload["timeline_slots"]
    assert [(r["clip_index"], r["split_panel_index"], r["split_offset_s"]) for r in rows] == [
        (0, 0, 0),
        (1, 0, 2),
        (0, 1, 0),
        (1, 1, 2),
    ]
    assert payload["split_screen_sections"][0]["duration_s"] == 4
    assert len({r["slot_id"] for r in rows}) == 4


def test_member_trim_preserves_canvas_text_in_section_gap(document):
    job, variant = document
    variant, _ = apply(
        job,
        variant,
        {"op": "create_split_screen", "slot_indexes": [0, 1], "panel_count": 2, "duration_s": 5},
    )
    variant["text_elements"][0].update(start_s=4, end_s=5)
    variant, _ = apply(
        job,
        variant,
        {"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": {"duration_s": 1}},
    )
    assert variant["text_elements"][0]["start_s"] == 4
    assert variant["text_elements"][0]["end_s"] == 5


def test_fit_to_video_uses_section_duration(document):
    job, variant = document
    for row in variant["user_timeline"]["slots"]:
        row["duration_s"] = 4
    variant, _ = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0, 1], "panel_count": 2}
    )
    variant["text_elements"][0]["source_params"] = {"sequence_source_id": "opening"}
    variant["text_elements"][0]["id"] = "opening::sequence-1"
    result = editor.compile_editor_ops(
        job,
        variant,
        [
            {
                "op": "retime_text_sequence",
                "selector": {"group": "all"},
                "target_ids": ["opening::sequence-1"],
                "expected_count": 1,
                "start_s": 0,
                "end_s": 8,
                "fit_to_video": True,
            }
        ],
    )
    assert result.canonical_duration_s == 4
    assert result.payload.text_elements[0]["end_s"] == 4


def test_section_duration_moves_downstream_text_once(document):
    job, variant = document
    tail = deepcopy(variant["user_timeline"]["slots"][1])
    tail["slot_id"] = "tail"
    variant["user_timeline"]["slots"].append(tail)
    variant, payload = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0, 1], "panel_count": 2}
    )
    variant["text_elements"][0].update(start_s=2, end_s=4, segment_id="tail")
    variant, _ = apply(
        job,
        variant,
        {
            "op": "update_split_screen",
            "section_id": payload["split_screen_sections"][0]["id"],
            "duration_s": 4,
        },
    )
    assert variant["text_elements"][0]["start_s"] == 4
    assert variant["text_elements"][0]["end_s"] == 6


def test_source_owned_label_follows_only_its_panel(document):
    job, variant = document
    variant, payload = apply(
        job,
        variant,
        {"op": "create_split_screen", "slot_indexes": [0, 1], "panel_count": 2, "duration_s": 4},
    )
    variant["text_elements"] = [
        {
            "id": "clip-label-top",
            "text": "Content",
            "segment_id": "slot-1",
            "start_s": 0,
            "end_s": 2,
        },
        {
            "id": "clip-label-bottom",
            "text": "Face",
            "segment_id": "slot-2",
            "start_s": 0,
            "end_s": 2,
        },
    ]
    variant, _ = apply(
        job,
        variant,
        {
            "op": "assign_split_panel",
            "section_id": payload["split_screen_sections"][0]["id"],
            "slot_index": 0,
            "panel_index": 0,
            "offset_s": 1,
        },
    )
    by_id = {row["id"]: row for row in variant["text_elements"]}
    assert by_id["clip-label-top"]["start_s"] == 1
    assert by_id["clip-label-top"]["end_s"] == 3
    assert by_id["clip-label-bottom"]["start_s"] == 0
    assert by_id["clip-label-bottom"]["end_s"] == 2


@pytest.mark.parametrize(
    "operation",
    [
        {
            "op": "create_split_screen",
            "slot_indexes": [0, 1],
            "panel_count": 2,
            "full_length": True,
        },
        {"op": "update_split_screen", "section_id": "s", "duration_s": 4, "panel_weights": [2, 1]},
        {
            "op": "assign_split_panel",
            "section_id": "s",
            "slot_index": 0,
            "panel_index": 1,
            "offset_s": 0.5,
        },
        {"op": "swap_split_panels", "section_id": "s", "first_panel": 0, "second_panel": 1},
        {"op": "remove_split_screen", "section_id": "s", "keep_panel": 0},
    ],
)
def test_provider_wire_retains_split_operation_fields(operation):
    from app.agents._creator_wire import WireContract, copilot_schema

    contract = WireContract(copilot_schema())
    payload = {"intent": "edit", "reply": "Prepared", "ops": [operation]}
    assert contract.decode(contract.encode(payload)) == payload


def test_frozen_panel_preserves_hold_and_source_split(document):
    job, variant = document
    row = variant["user_timeline"]["slots"][0]
    row.update(
        duration_s=4, source_duration_s=2, source_end_s=2, freeze_hold_s=2, freeze_frame_s=1.99
    )
    variant, payload = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    for row in payload["timeline_slots"][:2]:
        assert row["duration_s"] == 4
        assert row["freeze_hold_s"] == 2
        assert row["source_end_s"] == 2
    result = editor.compile_editor_ops(
        job, variant, [{"op": "split_clip", "slot_index": 0, "at_s": 1}]
    )
    left, right = result.payload.timeline_slots[:2]
    assert left.duration_s == 1
    assert left.freeze_hold_s == 0
    assert right.in_s == 1
    assert right.freeze_hold_s == 2
    assert right.freeze_frame_s == 1.99


def test_whole_output_trim_preserves_simultaneous_source_clocks(document):
    job, variant = document
    for row in variant["user_timeline"]["slots"]:
        row["duration_s"] = 4
    variant, _ = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0, 1], "panel_count": 2}
    )
    variant, payload = apply(job, variant, {"op": "trim_output_start", "start_s": 1})
    assert payload["split_screen_sections"][0]["duration_s"] == 3
    assert [r["in_s"] for r in payload["timeline_slots"]] == [1, 1]
    assert [r["split_offset_s"] for r in payload["timeline_slots"]] == [0, 0]
    assert editor.build_editor_snapshot(job, variant)["total_duration_s"] == 3


def test_whole_output_retarget_counts_section_once(document):
    job, variant = document
    variant, _ = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0, 1], "panel_count": 2}
    )
    variant, payload = apply(
        job, variant, {"op": "set_total_duration", "target_s": 4, "strategy": "proportional"}
    )
    assert payload["split_screen_sections"][0]["duration_s"] == 4
    assert editor.build_editor_snapshot(job, variant)["total_duration_s"] == 4


def test_frozen_speed_and_hold_split_do_not_move_peer(document, monkeypatch):
    job, variant = document
    monkeypatch.setattr(
        editor,
        "_editor_capabilities",
        lambda *args: {"timeline": True, "clips": {"playback_rate": True}},
    )
    first = variant["user_timeline"]["slots"][0]
    first.update(in_s=2, duration_s=4, source_end_s=4, freeze_hold_s=2, freeze_frame_s=4)
    variant, _ = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    peer = deepcopy(variant["user_timeline"]["slots"][1])
    variant, _ = apply(
        job,
        variant,
        {"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": {"playback_rate": 2}},
    )
    assert variant["user_timeline"]["slots"][0]["duration_s"] == 3
    assert variant["user_timeline"]["slots"][0]["freeze_hold_s"] == 2
    variant, _ = apply(job, variant, {"op": "split_clip", "slot_index": 0, "at_s": 2})
    right = variant["user_timeline"]["slots"][1]
    assert right["in_s"] == pytest.approx(4 - 1 / 30)
    assert right["freeze_frame_s"] == 4
    assert right["duration_s"] == 1
    assert variant["user_timeline"]["slots"][2] == peer


def test_frozen_slowdown_clamps_to_panel_without_moving_peer(document, monkeypatch):
    job, variant = document
    monkeypatch.setattr(
        editor,
        "_editor_capabilities",
        lambda *args: {"timeline": True, "clips": {"playback_rate": True}},
    )
    variant["user_timeline"]["slots"][0].update(
        in_s=2, duration_s=4, source_end_s=4, freeze_hold_s=2, freeze_frame_s=4
    )
    variant, _ = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    peer = deepcopy(variant["user_timeline"]["slots"][1])
    variant, _ = apply(
        job,
        variant,
        {"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": {"playback_rate": 0.5}},
    )
    first = variant["user_timeline"]["slots"][0]
    assert first["duration_s"] == 4
    assert first["freeze_hold_s"] == 0
    assert first["source_end_s"] == 4
    assert variant["user_timeline"]["slots"][1] == peer


def test_trim_to_background_only_refuses_atomically(document):
    job, variant = document
    variant, _ = apply(
        job,
        variant,
        {"op": "create_split_screen", "slot_indexes": [0, 1], "panel_count": 2, "duration_s": 5},
    )
    original = deepcopy(variant)
    with pytest.raises(
        editor.KriaEditorOpError, match="only an empty section background with no media occurrence"
    ):
        editor.compile_editor_ops(job, variant, [{"op": "trim_output_start", "start_s": 3}])
    assert variant == original


@pytest.mark.parametrize("strategy", ["uniform", "proportional", "trim_tail"])
def test_retarget_mixed_serial_and_panel_units(document, strategy):
    job, variant = document
    tail = deepcopy(variant["user_timeline"]["slots"][1])
    tail["slot_id"] = "tail"
    variant["user_timeline"]["slots"].append(tail)
    variant, _ = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0, 1], "panel_count": 2}
    )
    variant, payload = apply(
        job, variant, {"op": "set_total_duration", "target_s": 3, "strategy": strategy}
    )
    assert editor.build_editor_snapshot(job, variant)["total_duration_s"] == pytest.approx(3)
    assert payload["split_screen_sections"]


def test_split_member_uses_projected_output_time_with_fullscreen_prefix(document):
    job, variant = document
    prefix = deepcopy(variant["user_timeline"]["slots"][1])
    prefix.update(slot_id="prefix", duration_s=3)
    variant["user_timeline"]["slots"].insert(0, prefix)
    variant, _ = apply(
        job,
        variant,
        {"op": "create_split_screen", "slot_indexes": [1, 2], "panel_count": 2, "duration_s": 4},
    )
    variant, payload = apply(job, variant, {"op": "split_clip", "slot_index": 1, "at_s": 4})
    left, right = payload["timeline_slots"][1:3]
    assert left["duration_s"] == 1
    assert right["in_s"] == 1
    assert right["split_offset_s"] == 1


def test_panel_duration_extension_does_not_reserve_another_panels_source(document):
    job, variant = document
    variant["user_timeline"]["slots"][0].update(source_duration_s=4, media_id="content")
    variant, payload = apply(
        job,
        variant,
        {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2, "duration_s": 4},
    )
    assert variant["user_timeline"]["slots"][1]["source_duration_s"] == 4
    assert variant["user_timeline"]["slots"][1]["media_id"] == "content"
    variant["user_timeline"]["slots"][1]["in_s"] = 2
    peer = deepcopy(variant["user_timeline"]["slots"][0])
    variant, _ = apply(
        job,
        variant,
        {"op": "patch_slots", "selector": {"slot_indexes": [1]}, "patch": {"duration_s": 3}},
    )
    assert variant["user_timeline"]["slots"][1]["duration_s"] == 3
    assert variant["user_timeline"]["slots"][1]["in_s"] == 1
    assert variant["user_timeline"]["slots"][0] == peer


@pytest.mark.parametrize(("gain", "expected"), [(0, 1), (0.4, 0.4), (1, 1)])
def test_explicit_panel_unmute_restores_zero_gain(document, monkeypatch, gain, expected):
    job, variant = document
    monkeypatch.setattr(
        editor, "_editor_capabilities", lambda *args: {"timeline": True, "clips": {"audio": True}}
    )
    variant["user_timeline"]["slots"][0].update(muted=True, source_audio_volume=gain)
    variant, _ = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    peer = deepcopy(variant["user_timeline"]["slots"][1])
    variant, payload = apply(
        job,
        variant,
        {"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": {"muted": False}},
    )
    assert payload["timeline_slots"][0]["muted"] is False
    assert payload["timeline_slots"][0]["source_audio_volume"] == expected
    assert variant["user_timeline"]["slots"][1] == peer


def test_provider_wire_preserves_explicit_unmute():
    from app.agents._creator_wire import WireContract, copilot_schema

    contract = WireContract(copilot_schema())
    payload = {
        "intent": "edit",
        "reply": "Prepared",
        "ops": [
            {"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": {"muted": False}}
        ],
    }
    assert contract.decode(contract.encode(payload)) == payload


@pytest.mark.parametrize("gain", [0, 0.5, 2])
def test_panel_gain_controls_restore_sound_without_changing_peer(document, monkeypatch, gain):
    job, variant = document
    monkeypatch.setattr(
        editor, "_editor_capabilities", lambda *args: {"timeline": True, "clips": {"audio": True}}
    )
    variant["user_timeline"]["slots"][0].update(muted=True, source_audio_volume=0)
    variant, _ = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    peer = deepcopy(variant["user_timeline"]["slots"][1])
    variant, payload = apply(
        job,
        variant,
        {
            "op": "patch_slots",
            "selector": {"slot_indexes": [0]},
            "patch": {"source_audio_volume": gain},
        },
    )
    assert payload["timeline_slots"][0]["source_audio_volume"] == gain
    if gain > 0:
        assert payload["timeline_slots"][0]["muted"] is False
    assert variant["user_timeline"]["slots"][1] == peer


@pytest.mark.parametrize("gain", [-0.1, 2.1, True, "0.5", float("nan")])
def test_invalid_panel_gain_refused(document, monkeypatch, gain):
    job, variant = document
    monkeypatch.setattr(
        editor, "_editor_capabilities", lambda *args: {"timeline": True, "clips": {"audio": True}}
    )
    snapshot = editor.build_editor_snapshot(job, variant)
    snapshot["editor_ops_version"] = 2
    assert (
        parse_editor_operation(
            {
                "op": "patch_slots",
                "selector": {"slot_indexes": [0]},
                "patch": {"source_audio_volume": gain},
            },
            snapshot,
            _ParseState(0.9),
        )
        is None
    )


def test_panel_audio_respects_closed_capability(document):
    job, variant = document
    snapshot = editor.build_editor_snapshot(job, variant)
    snapshot["editor_ops_version"] = 2
    for patch in ({"muted": False}, {"source_audio_volume": 0.5}):
        assert (
            parse_editor_operation(
                {"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": patch},
                snapshot,
                _ParseState(0.9),
            )
            is None
        )
        with pytest.raises(editor.KriaEditorOpError, match="not available"):
            editor.compile_editor_ops(
                job,
                variant,
                [{"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": patch}],
            )
