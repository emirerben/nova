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


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("explicit", [None, False, True])
def test_legacy_speaker_provenance_survives_copy_and_unstack(document, bounded, explicit):
    job, variant = document
    row = variant["user_timeline"]["slots"][0]
    row.update(slot_id="native-phone-talking-source-a", duration_s=4)
    if explicit is not None:
        row["speaker_subject"] = explicit
    command = {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    if bounded:
        command.update(start_s=1, end_s=3)
    variant, payload = apply(job, variant, command)
    expected = explicit if explicit is not None else True
    copied = [r for r in payload["timeline_slots"] if r["clip_index"] == 0]
    assert all(r.get("speaker_subject") is expected for r in copied)
    assert all(not r.get("parent_segment_id") for r in copied)
    section = payload["split_screen_sections"][0]["id"]
    variant, payload = apply(
        job, variant, {"op": "remove_split_screen", "section_id": section, "keep_panel": 1}
    )
    copied = [r for r in variant["user_timeline"]["slots"] if r["clip_index"] == 0]
    assert all(r.get("speaker_subject") is expected for r in copied)


def test_legacy_speaker_provenance_on_assignment_and_panel_expansion(document):
    job, variant = document
    rows = variant["user_timeline"]["slots"]
    rows[0].update(slot_id="native-phone-talking-source-a", duration_s=4)
    rows[1].update(slot_id="native-phone-talking-source-b", duration_s=4, speaker_subject=False)
    variant, payload = apply(
        job,
        variant,
        {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2, "duration_s": 8},
    )
    section = payload["split_screen_sections"][0]["id"]
    variant, payload = apply(
        job, variant, {"op": "update_split_screen", "section_id": section, "panel_count": 3}
    )
    assert all(
        r.get("speaker_subject") is True for r in payload["timeline_slots"] if r["clip_index"] == 0
    )
    source_index = next(
        i
        for i, r in enumerate(editor.build_editor_snapshot(job, variant)["slots"])
        if r["clip_index"] == 1
    )
    variant, payload = apply(
        job,
        variant,
        {
            "op": "assign_split_panel",
            "section_id": section,
            "slot_index": source_index,
            "panel_index": 1,
            "offset_s": 4,
            "duplicate": True,
        },
    )
    assigned = next(
        r for r in payload["timeline_slots"] if r["clip_index"] == 1 and r.get("split_section_id")
    )
    assert assigned["speaker_subject"] is False
    assert not assigned.get("parent_segment_id")


def _transition_document(document, monkeypatch, offsets=(0, 2, 4), duration=7):
    job, variant = document
    monkeypatch.setattr(
        editor,
        "_editor_capabilities",
        lambda *args: {"timeline": True, "clips": {"transitions": True}},
    )
    from app.config import settings

    monkeypatch.setattr(settings, "edit_transitions_enabled", True)
    source = variant["user_timeline"]["slots"][0]
    source.update(media_kind="video", look_preset="none")
    rows = [
        {
            **deepcopy(source),
            "slot_id": f"clip-{i}",
            "duration_s": 2,
            "split_section_id": "s",
            "split_panel_index": 0,
            "split_offset_s": offset,
        }
        for i, offset in enumerate(offsets)
    ]
    rows.append(
        {
            **deepcopy(source),
            "slot_id": "peer",
            "duration_s": duration,
            "split_section_id": "s",
            "split_panel_index": 1,
            "split_offset_s": 0,
        }
    )
    variant["user_timeline"] = {
        "slots": rows,
        "split_screen_sections": [
            {"id": "s", "duration_s": duration, "panel_count": 2, "panel_weights": [1, 1]}
        ],
    }
    return job, variant


def _transition_op(api, transition, seconds=0.2):
    return (
        {
            "op": "set_transition",
            "boundary_index": 0,
            "transition": transition,
            "duration_s": seconds,
        }
        if api == "set_transition"
        else {
            "op": "patch_slots",
            "selector": {"slot_indexes": [0]},
            "patch": {"transition_after": transition, "transition_duration_s": seconds},
        }
    )


@pytest.mark.parametrize("api", ["set_transition", "patch_slots"])
def test_panel_transition_moves_same_panel_suffix_only(document, monkeypatch, api):
    job, variant = _transition_document(document, monkeypatch)
    peer = deepcopy(variant["user_timeline"]["slots"][-1])
    updated, _ = apply(job, variant, _transition_op(api, "crossfade"))
    rows = updated["user_timeline"]["slots"]
    assert [r["split_offset_s"] for r in rows[:3]] == pytest.approx([0, 1.8, 3.8])
    assert rows[-1] == peer
    assert [r["in_s"] for r in rows[:3]] == [0, 0, 0]
    assert updated["user_timeline"]["split_screen_sections"][0]["duration_s"] == 7


@pytest.mark.parametrize("api", ["set_transition", "patch_slots"])
def test_panel_transition_cut_restores_suffix_without_source_retime(document, monkeypatch, api):
    job, variant = _transition_document(document, monkeypatch, offsets=(0, 1.8, 3.8), duration=6)
    variant["user_timeline"]["slots"][0].update(
        transition_after="crossfade", transition_duration_s=0.2
    )
    updated, _ = apply(job, variant, _transition_op(api, "cut"))
    assert [r["split_offset_s"] for r in updated["user_timeline"]["slots"][:3]] == pytest.approx(
        [0, 2, 4]
    )


@pytest.mark.parametrize("api", ["set_transition", "patch_slots"])
def test_panel_transition_preserves_deliberate_gap(document, monkeypatch, api):
    job, variant = _transition_document(document, monkeypatch, offsets=(0, 2.5, 4.5))
    updated, _ = apply(job, variant, _transition_op(api, "crossfade"))
    assert [r["split_offset_s"] for r in updated["user_timeline"]["slots"][:3]] == [0, 2.5, 4.5]


@pytest.mark.parametrize("api", ["set_transition", "patch_slots"])
def test_panel_transition_bounds_refuse_atomically(document, monkeypatch, api):
    job, variant = _transition_document(document, monkeypatch, offsets=(0, 1.8, 3.8), duration=5.8)
    variant["user_timeline"]["slots"][0].update(
        transition_after="crossfade", transition_duration_s=0.2
    )
    before = deepcopy(variant)
    with pytest.raises(editor.KriaEditorOpError):
        apply(job, variant, _transition_op(api, "cut"))
    assert variant == before


def test_layout_only_update_preserves_unconsumed_source_eof_and_properties(document):
    job, variant = document
    variant, _ = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    row = variant["user_timeline"]["slots"][0]
    row.update(
        source_end_s=4,
        duration_s=2,
        playback_rate=1,
        source_audio_volume=0.4,
        speaker_subject=True,
        media_transform={"scale": 0.8, "position_x": 12, "position_y": -9, "rotation_degrees": 0},
    )
    before = deepcopy(variant["user_timeline"]["slots"])
    section_id = variant["user_timeline"]["split_screen_sections"][0]["id"]
    updated, _ = apply(
        job,
        variant,
        {"op": "update_split_screen", "section_id": section_id, "panel_weights": [2, 1]},
    )
    assert updated["user_timeline"]["slots"] == before


def test_full_length_creation_preserves_serial_overlap_clock(document, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "edit_transitions_enabled", True)
    job, variant = document
    variant["user_timeline"]["slots"][0].update(
        transition_after="crossfade", transition_duration_s=0.2
    )
    updated, payload = apply(
        job,
        variant,
        {
            "op": "create_split_screen",
            "slot_indexes": [0, 1],
            "panel_count": 2,
            "full_length": True,
        },
    )
    assert payload["split_screen_sections"][0]["duration_s"] == pytest.approx(3.8)
    rows = updated["user_timeline"]["slots"]
    assert [r["split_offset_s"] for r in rows] == pytest.approx([0, 1.8, 0, 1.8])
    assert rows[0]["transition_after"] == rows[2]["transition_after"] == "crossfade"
    assert [r["duration_s"] for r in rows] == [2, 2, 2, 2]


def test_duration_update_cannot_silently_delete_background_only_section(document):
    job, variant = document
    row, tail = deepcopy(variant["user_timeline"]["slots"])
    row.update(duration_s=1, split_section_id="s", split_panel_index=0, split_offset_s=3)
    variant["user_timeline"] = {
        "slots": [row, tail],
        "split_screen_sections": [
            {"id": "s", "duration_s": 4, "panel_count": 2, "panel_weights": [1, 1]}
        ],
    }
    before = deepcopy(variant)
    with pytest.raises(editor.KriaEditorOpError, match="empty section background"):
        apply(job, variant, {"op": "update_split_screen", "section_id": "s", "duration_s": 2})
    assert variant == before


@pytest.mark.parametrize("api", ["set_transition", "patch_slots"])
@pytest.mark.parametrize("last_offset, following_transition", [(3.8, "crossfade"), (4.5, "cut")])
def test_panel_transition_preserves_later_join_or_gap(
    document, monkeypatch, api, last_offset, following_transition
):
    job, variant = _transition_document(document, monkeypatch, offsets=(0, 2, last_offset))
    variant["user_timeline"]["slots"][1].update(
        transition_after=following_transition, transition_duration_s=0.2
    )
    before = deepcopy(variant["user_timeline"]["slots"])
    updated, _ = apply(job, variant, _transition_op(api, "crossfade"))
    rows = updated["user_timeline"]["slots"]
    assert rows[2]["split_offset_s"] - rows[1]["split_offset_s"] == pytest.approx(last_offset - 2)
    assert rows[1]["transition_after"] == following_transition
    for old, new in zip(before, rows, strict=True):
        assert (
            new["in_s"],
            new["duration_s"],
            new.get("source_end_s"),
            new.get("playback_rate"),
        ) == (old["in_s"], old["duration_s"], old.get("source_end_s"), old.get("playback_rate"))


def test_explicit_delete_final_section_member_remains_allowed(document):
    job, variant = document
    row, tail = deepcopy(variant["user_timeline"]["slots"])
    row.update(duration_s=1, split_section_id="s", split_panel_index=0, split_offset_s=3)
    variant["user_timeline"] = {
        "slots": [row, tail],
        "split_screen_sections": [
            {"id": "s", "duration_s": 4, "panel_count": 2, "panel_weights": [1, 1]}
        ],
    }
    updated, payload = apply(job, variant, {"op": "remove_clip", "slot_index": 0})
    assert payload["split_screen_sections"] == []
    assert [r["slot_id"] for r in updated["user_timeline"]["slots"] if not r.get("removed")] == [
        tail["slot_id"]
    ]


def test_bulk_panel_transitions_validate_final_candidate(document, monkeypatch):
    job, variant = _transition_document(document, monkeypatch, offsets=(0, 1.7, 3.6), duration=5.6)
    rows = variant["user_timeline"]["slots"]
    rows[0].update(transition_after="crossfade", transition_duration_s=0.3)
    rows[1].update(transition_after="crossfade", transition_duration_s=0.1)
    updated, _ = apply(
        job,
        variant,
        {
            "op": "patch_slots",
            "selector": {"slot_indexes": [0, 1]},
            "patch": {"transition_after": "crossfade", "transition_duration_s": 0.2},
        },
    )
    assert [
        row["split_offset_s"] for row in updated["user_timeline"]["slots"][:3]
    ] == pytest.approx([0, 1.8, 3.6])


@pytest.mark.parametrize("api", ["set_transition", "patch_slots"])
def test_panel_transition_uses_clock_not_storage_order(document, monkeypatch, api):
    job, variant = _transition_document(document, monkeypatch)
    rows = variant["user_timeline"]["slots"]
    variant["user_timeline"]["slots"] = rows[1:] + rows[:1]
    op = _transition_op(api, "crossfade")
    if api == "set_transition":
        op["boundary_index"] = 3
    else:
        op["selector"] = {"slot_indexes": [3]}
    updated, _ = apply(job, variant, op)
    rows = updated["user_timeline"]["slots"]
    assert [rows[i]["split_offset_s"] for i in [3, 0, 1]] == pytest.approx([0, 1.8, 3.8])


# Parity failure modes: minted IDs inherit caption ownership; replacement retains stale
# source clocks; sequential panel assignments collide; section moves tear the group.
def test_duplicate_ordinary_keeps_properties_without_caption_parent(document):
    job, variant = document
    original = deepcopy(variant["user_timeline"]["slots"][0])
    original["parent_segment_id"] = "old-parent"
    variant["user_timeline"]["slots"][0] = original
    result, payload = apply(job, variant, {"op": "duplicate_clip", "slot_index": 0})
    rows = result["user_timeline"]["slots"]
    assert rows[1]["slot_id"] != rows[0]["slot_id"]
    assert not rows[1].get("parent_segment_id")
    for key in ("clip_index", "source_crop", "muted", "duration_s"):
        assert rows[1][key] == original[key]


def test_replace_source_keeps_occurrence_appearance_and_resets_clock(document):
    job, variant = document
    rows = variant["user_timeline"]["slots"]
    rows[0].update(in_s=1, freeze_hold_s=1, freeze_frame_s=3, source_audio_volume=0.5)
    job.all_candidates["clip_paths"][1] = "approved.mp4"
    rows[1]["source_duration_s"] = 2
    identity, crop = rows[0]["slot_id"], deepcopy(rows[0]["source_crop"])
    result, payload = apply(
        job, variant, {"op": "replace_clip_source", "slot_index": 0, "clip_index": 1}
    )
    row = result["user_timeline"]["slots"][0]
    assert row["slot_id"] == identity and row["clip_index"] == 1
    assert row["source_crop"] == crop and row["source_audio_volume"] == 0.5
    assert row["in_s"] == 0 and row["duration_s"] <= 2
    assert row.get("freeze_hold_s", 0) == 0 and row.get("speaker_subject") is False


def test_atomic_panel_and_section_moves_preserve_gap_and_peers(document):
    job, variant = document
    variant["user_timeline"]["slots"][0]["duration_s"] = 2
    variant, payload = apply(
        job,
        variant,
        {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2, "duration_s": 8},
    )
    section = payload["split_screen_sections"][0]["id"]
    variant, _ = apply(job, variant, {"op": "duplicate_clip", "slot_index": 0})
    rows = variant["user_timeline"]["slots"]
    rows[1]["split_offset_s"] = 3
    rows[1]["duration_s"] = 3
    peer = deepcopy(rows[2])
    variant, _ = apply(
        job, variant, {"op": "move_split_clip", "slot_index": 0, "direction": "later"}
    )
    rows = variant["user_timeline"]["slots"]
    assert [r["split_offset_s"] for r in rows[:2]] == [4, 0]
    assert rows[2] == peer
    variant, _ = apply(
        job, variant, {"op": "move_split_screen", "section_id": section, "direction": "later"}
    )
    rows = variant["user_timeline"]["slots"]
    assert not rows[0].get("split_section_id")
    assert all(r["split_section_id"] == section for r in rows[1:])


@pytest.mark.parametrize("photo", [False, True])
def test_replace_source_clears_stale_metadata_preserves_scoped_frame(document, photo):
    job, variant = document
    rows = variant["user_timeline"]["slots"]
    rows[0].update(
        source_end_s=4,
        source_gcs_path="old-path",
        freeze_frame_s=4,
        freeze_hold_s=1,
        media_transform={"scale": 1.2},
        speaker_frame_override={"mode": "full"},
    )
    job.all_candidates["clip_paths"][1] = "approved.mp4"
    rows[1]["source_duration_s"] = 8
    if photo:
        job.all_candidates["clip_paths"][1] = "approved.jpg"
        before = deepcopy(variant)
        with pytest.raises(editor.KriaEditorOpError, match="default framing"):
            apply(job, variant, {"op": "replace_clip_source", "slot_index": 0, "clip_index": 1})
        assert variant == before
        rows[0]["speaker_frame_override"] = {"mode": "inherit"}
    result, payload = apply(
        job, variant, {"op": "replace_clip_source", "slot_index": 0, "clip_index": 1}
    )
    row = result["user_timeline"]["slots"][0]
    assert row.get("source_gcs_path") != "old-path"
    assert not row.get("freeze_frame_s") and not row.get("source_end_s")
    assert row["speaker_subject"] is (not photo)
    assert row["speaker_frame_override"] == rows[0]["speaker_frame_override"]


def test_duplicate_split_refuses_collision_atomically_and_gate_closed(document, monkeypatch):
    job, variant = document
    variant, _ = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    before = deepcopy(variant)
    with pytest.raises(editor.KriaEditorOpError):
        apply(job, variant, {"op": "duplicate_clip", "slot_index": 0})
    assert variant == before
    monkeypatch.setattr(editor, "_editor_capabilities", lambda *args: {"timeline": False})
    snapshot = editor.build_editor_snapshot(job, variant)
    assert (
        parse_editor_operation(
            {"op": "duplicate_clip", "slot_index": 0}, snapshot, _ParseState(0.99)
        )
        is None
    )
    with pytest.raises(editor.KriaEditorOpError, match="unavailable"):
        editor.compile_editor_ops(job, variant, [{"op": "duplicate_clip", "slot_index": 0}])


def test_duplicate_preserves_held_source_clock_and_source_admission(document):
    job, variant = document
    row = variant["user_timeline"]["slots"][0]
    row.update(
        duration_s=4, in_s=0, source_duration_s=2, source_end_s=2, freeze_frame_s=2, freeze_hold_s=2
    )
    variant, _ = apply(job, variant, {"op": "duplicate_clip", "slot_index": 0})
    rows = variant["user_timeline"]["slots"]
    for key in ("in_s", "duration_s", "source_end_s", "freeze_frame_s", "freeze_hold_s"):
        assert rows[0][key] == rows[1][key]
    snapshot = editor.build_editor_snapshot(job, variant)
    snapshot["editor_ops_version"] = 2
    assert (
        parse_editor_operation(
            {"op": "replace_clip_source", "slot_index": 0, "clip_index": 999},
            snapshot,
            _ParseState(0.99),
        )
        is None
    )
    before = deepcopy(variant)
    with pytest.raises(editor.KriaEditorOpError, match="approved"):
        editor.compile_editor_ops(
            job, variant, [{"op": "replace_clip_source", "slot_index": 0, "clip_index": 999}]
        )
    assert variant == before


def test_move_two_sections_retains_every_member_and_section(document):
    job, variant = document
    variant, payload = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    first = payload["split_screen_sections"][0]["id"]
    variant, payload = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [2], "panel_count": 2}
    )
    sections = deepcopy(variant["user_timeline"]["split_screen_sections"])
    rows = deepcopy(variant["user_timeline"]["slots"])
    variant, _ = apply(
        job, variant, {"op": "move_split_screen", "section_id": first, "direction": "later"}
    )
    assert variant["user_timeline"]["slots"] == rows[2:] + rows[:2]
    assert variant["user_timeline"]["split_screen_sections"] == sections


@pytest.mark.parametrize("inside", [False, True])
def test_section_move_ignores_removed_tombstones(document, inside):
    job, variant = document
    variant, payload = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    section = payload["split_screen_sections"][0]["id"]
    rows = variant["user_timeline"]["slots"]
    tombstone = {**deepcopy(rows[-1]), "slot_id": "removed-occurrence", "removed": True}
    rows.insert(1 if inside else 2, tombstone)
    variant, _ = apply(
        job, variant, {"op": "move_split_screen", "section_id": section, "direction": "later"}
    )
    live = [r for r in variant["user_timeline"]["slots"] if not r.get("removed")]
    assert not live[0].get("split_section_id")
    assert all(r.get("split_section_id") == section for r in live[1:])
    assert tombstone in variant["user_timeline"]["slots"]


def test_generic_custom_reorder_groups_section_members(document):
    job, variant = document
    variant, _ = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    original = deepcopy(variant["user_timeline"]["slots"])
    variant, _ = apply(
        job, variant, {"op": "reorder_clips_by", "criterion": "custom", "order": [2, 1, 0]}
    )
    assert variant["user_timeline"]["slots"] == original[2:] + original[:2]


def test_section_reorder_moves_canvas_title_and_sound(document):
    job, variant = document
    variant, payload = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    section = payload["split_screen_sections"][0]["id"]
    following_duration = variant["user_timeline"]["slots"][-1]["duration_s"]
    variant["text_elements"] = [
        {"id": "canvas-title", "text": "Title", "start_s": 0.5, "end_s": 1.5}
    ]
    variant["sound_effects"] = [{"id": "canvas-sound", "sfx_id": "pop", "at_s": 1, "volume": 1}]
    variant["media_overlays"] = [{"id": "canvas-overlay", "start_s": 0.5, "end_s": 1.5}]
    result, payload = apply(
        job, variant, {"op": "move_split_screen", "section_id": section, "direction": "later"}
    )
    assert result["text_elements"][0]["start_s"] == 0.5 + following_duration
    assert result["sound_effects"][0]["at_s"] == 1 + following_duration
    assert result["media_overlays"][0]["start_s"] == 0.5 + following_duration


def test_replace_admits_ready_import_and_uses_original_receipt_duration(document):
    from app.services.phone_sources import PHONE_SOURCES_FIELD, bind_phone_sources
    from tests.services.test_phone_sources import receipt

    job, variant = document
    receipts = [receipt("one"), receipt("two"), receipt("imported")]
    bindings = bind_phone_sources(receipts, [r["gcs_path"] for r in receipts])
    job.all_candidates["clip_paths"] = [r["gcs_path"] for r in receipts[:2]]
    job.assembly_plan[PHONE_SOURCES_FIELD] = [b.model_dump(mode="json") for b in bindings[:2]]
    variant["_editor_sources_v1"] = {
        "sources": [
            {
                "source_index": 2,
                "status": "ready",
                "media_id": "imported",
                "lane": "clip",
                "gcs_path": receipts[2]["gcs_path"],
                "generation": "123",
                "kind": "video",
                "duration_s": 10,
                "source_binding": bindings[2].model_dump(mode="json"),
            }
        ]
    }
    pool = editor.replacement_source_pool(job, variant)
    assert pool[1]["duration_s"] == 10  # receipt beats the saved 2-second timeline hint
    assert "proxy" not in str(pool)
    result, _ = apply(job, variant, {"op": "replace_clip_source", "slot_index": 0, "clip_index": 2})
    row = result["user_timeline"]["slots"][0]
    assert row["clip_index"] == 2 and row["media_id"] == "imported"
    assert row["source_duration_s"] == 10
    variant["_editor_sources_v1"]["sources"][0]["status"] = "preparing"
    snapshot = editor.build_editor_snapshot(job, variant)
    snapshot["editor_ops_version"] = 2
    assert (
        parse_editor_operation(
            {"op": "replace_clip_source", "slot_index": 0, "clip_index": 2},
            snapshot,
            _ParseState(0.99),
        )
        is None
    )


@pytest.mark.parametrize("criterion", ["capture_time", "random"])
def test_generic_automatic_reorder_keeps_section_unit(document, criterion):
    job, variant = document
    variant, _ = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    before = deepcopy(variant["user_timeline"]["slots"])
    snapshot = editor.build_editor_snapshot(job, variant)
    snapshot["editor_ops_version"] = 2
    for index, row in enumerate(snapshot["slots"]):
        row["facts"] = [{"kind": "capture_time", "value": f"2026-09-{23 - index}T07:00:00Z"}]
    parsed = parse_editor_operation(
        {"op": "reorder_clips_by", "criterion": criterion}, snapshot, _ParseState(0.99)
    )
    assert parsed is not None
    draft = editor.compile_editor_ops(job, variant, [parsed])
    result = editor.project_editor_draft(
        variant, draft.payload.model_dump(mode="json", exclude_none=True), job
    )
    members = [row for row in result["user_timeline"]["slots"] if row.get("split_section_id")]
    assert members == before[:2]
    indices = [
        i for i, r in enumerate(result["user_timeline"]["slots"]) if r.get("split_section_id")
    ]
    assert indices[1] == indices[0] + 1


@pytest.mark.parametrize("explicit", [False, True])
def test_section_canvas_move_honors_protected_text(document, explicit):
    job, variant = document
    variant, payload = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    section = payload["split_screen_sections"][0]["id"]
    variant["text_elements"] = [{"id": "title", "text": "Title", "start_s": 0.2, "end_s": 0.8}]
    operations = [{"op": "move_split_screen", "section_id": section, "direction": "later"}]
    if explicit:
        snapshot = editor.build_editor_snapshot(job, variant)
        snapshot["editor_ops_version"] = 2
        snapshot["allowed_op_families"].append("text")
        parsed = parse_editor_operation(
            {"op": "set_texts_timing", "selector": {"group": "all"}, "start_s": 0.4, "end_s": 1.2},
            snapshot,
            _ParseState(0.99),
        )
        assert parsed is not None
        operations.append(parsed)
    draft = editor.compile_editor_ops(job, variant, operations, preserve_text=not explicit)
    row = draft.payload.text_elements[0]
    assert row["start_s"] == (0.4 if explicit else 0.2)
    assert row["end_s"] == (1.2 if explicit else 0.8)


def test_section_move_rebases_drawings_background_and_manual_captions(document):
    job, variant = document
    variant, payload = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    section = payload["split_screen_sections"][0]["id"]
    variant["drawings"] = [{"id": "drawing", "start_s": 0.2, "end_s": 0.8}]
    variant["caption_cues"] = [
        {
            "id": "cue",
            "text": "Hello",
            "start_s": 0.2,
            "end_s": 0.8,
            "words": [{"text": "Hello", "start_s": 0.2, "end_s": 0.8}],
        }
    ]
    variant["speaker_background"] = {
        "kind": "color",
        "color": "#000000",
        "windows": [{"start_s": 0.2, "end_s": 0.8, "media_id": "background", "kind": "image"}],
    }
    draft = editor.compile_editor_ops(
        job, variant, [{"op": "move_split_screen", "section_id": section, "direction": "later"}]
    )
    payload = draft.payload.model_dump(mode="json", exclude_none=True)
    assert payload["drawings"][0]["start_s"] == 2.2
    assert payload["caption_cues"][0]["start_s"] == 2.2
    assert payload["caption_cues"][0]["words"][0]["end_s"] == 2.8
    assert payload["speaker_background"]["windows"][0]["start_s"] == 2.2


def test_section_move_keeps_explicit_sfx_time_and_rebases_unchanged_peer(document):
    job, variant = document
    variant, payload = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    section = payload["split_screen_sections"][0]["id"]
    variant["sound_effects"] = [
        {"id": "explicit", "sfx_id": "pop", "at_s": 0.3, "volume": 1},
        {"id": "follow", "sfx_id": "pop", "at_s": 0.4, "volume": 1},
    ]
    draft = editor.compile_editor_ops(
        job,
        variant,
        [
            {"op": "move_split_screen", "section_id": section, "direction": "later"},
            {"op": "patch_sfx", "sfx_index": 0, "at_s": 0.7},
        ],
    )
    assert draft.payload.sound_effects[0]["at_s"] == 0.7
    assert draft.payload.sound_effects[1]["at_s"] == 2.4
    old, new = draft.timeline_rebase["sound_effects"]
    assert [r["at_s"] for r in old] == [0.7, 0.4]
    assert [r["at_s"] for r in new] == [0.7, 2.4]
    assert "timeline_rebase" not in draft.payload.model_dump()


def test_section_move_does_not_rebase_speech_captions_on_minted_source(document):
    job, variant = document
    variant["user_timeline"]["slots"][0].update(
        slot_id="minted-uuid",
        parent_segment_id="native-phone-talking-source-a",
        speaker_subject=True,
    )
    variant, payload = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    section = payload["split_screen_sections"][0]["id"]
    variant["caption_cues"] = [{"id": "speech", "text": "Hello", "start_s": 0.2, "end_s": 0.8}]
    result, _ = apply(
        job, variant, {"op": "move_split_screen", "section_id": section, "direction": "later"}
    )
    assert result["caption_cues"] == variant["caption_cues"]


def test_section_move_respects_explicit_camera_overlay_and_background_sequence(document):
    job, variant = document
    variant, payload = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    section = payload["split_screen_sections"][0]["id"]
    variant["media_overlays"] = [{"id": "overlay", "start_s": 0.1, "end_s": 0.5}]
    variant["camera_effects"] = [{"id": "camera", "start_s": 0.1, "end_s": 0.5}]
    sequence = {
        "op": "set_background_sequence",
        "asset_ids": ["one", "two"],
        "kinds": ["image", "image"],
        "cadence_s": 1,
    }
    expected = editor.compile_editor_ops(job, variant, [sequence]).payload.speaker_background
    draft = editor.compile_editor_ops(
        job,
        variant,
        [
            {"op": "move_split_screen", "section_id": section, "direction": "later"},
            {"op": "patch_overlay", "overlay_index": 0, "patch": {"start_s": 0.6, "end_s": 1.3}},
            {"op": "patch_camera_effect", "camera_effect_index": 0, "start_s": 0.7, "end_s": 1.4},
            sequence,
        ],
    )
    assert draft.payload.media_overlays[0]["start_s"] == 0.6
    assert draft.payload.camera_effects[0]["start_s"] == 0.7
    assert draft.payload.speaker_background == expected
    assert (
        not {"media_overlays", "camera_effects", "speaker_background"}
        & draft.timeline_rebase.keys()
    )


def test_canvas_follows_section_creation_extension_and_unstack(document):
    job, variant = document
    variant["drawings"] = [{"id": "drawing", "start_s": 2.2, "end_s": 2.8}]
    variant["speaker_background"] = {
        "kind": "color",
        "color": "#000000",
        "windows": [{"media_id": "bg", "kind": "image", "start_s": 2.2, "end_s": 2.8}],
    }
    variant, payload = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0, 1], "panel_count": 2}
    )
    assert variant["drawings"][0]["start_s"] == 0.2
    assert variant["speaker_background"]["windows"][0]["start_s"] == 0.2
    section = payload["split_screen_sections"][0]["id"]
    variant, _ = apply(
        job, variant, {"op": "remove_split_screen", "section_id": section, "keep_panel": 1}
    )
    assert variant["drawings"][0]["start_s"] == 0.2


@pytest.mark.parametrize("duration_s", [None, 4])
@pytest.mark.parametrize("keep_panel", [0, 1])
def test_unstack_keeps_text_over_the_panel_that_survives(document, keep_panel, duration_s):
    job, variant = document
    assert [(t["text"], t["start_s"], t["end_s"]) for t in variant["text_elements"]] == [
        ("Old hook", 0.0, 2.0)
    ]
    create = {"op": "create_split_screen", "slot_indexes": [0, 1], "panel_count": 2}
    if duration_s is not None:
        create["duration_s"] = duration_s
    variant, payload = apply(job, variant, create)
    section = payload["split_screen_sections"][0]["id"]
    variant, _ = apply(
        job,
        variant,
        {"op": "remove_split_screen", "section_id": section, "keep_panel": keep_panel},
    )
    assert [t["text"] for t in variant["text_elements"] if not t.get("removed")] == ["Old hook"]


def test_section_extension_moves_following_canvas(document):
    job, variant = document
    variant, payload = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    section = payload["split_screen_sections"][0]["id"]
    variant["drawings"] = [{"id": "drawing", "start_s": 2.2, "end_s": 2.8}]
    variant, _ = apply(
        job, variant, {"op": "update_split_screen", "section_id": section, "duration_s": 4}
    )
    assert variant["drawings"][0]["start_s"] == 4.2
    variant, _ = apply(
        job, variant, {"op": "remove_split_screen", "section_id": section, "keep_panel": 0}
    )
    assert variant["drawings"][0]["start_s"] == 2.2


def test_removing_section_drops_canvas_interval_without_surviving_unit(document):
    job, variant = document
    variant, _ = apply(
        job, variant, {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2}
    )
    variant["drawings"] = [{"id": "removed-drawing", "start_s": 0.2, "end_s": 0.8}]
    result, _ = apply(
        job,
        variant,
        {"op": "patch_slots", "selector": {"slot_indexes": [0, 1]}, "patch": {"removed": True}},
    )
    assert result["drawings"] == []


def test_ordinary_timeline_edit_does_not_claim_split_rebase_dependencies(document, monkeypatch):
    job, variant = document
    assert not editor._variant_split_sections(variant)
    monkeypatch.setattr(
        editor,
        "_editor_capabilities",
        lambda *args: {"timeline": True, "clips": {"playback_rate": True}},
    )
    draft = editor.compile_editor_ops(
        job,
        variant,
        [{"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": {"playback_rate": 2}}],
    )
    assert draft.timeline_rebase == {}


def test_split_shrink_removes_partial_background_windows_below_minimum(document):
    job, variant = document
    variant, payload = apply(
        job,
        variant,
        {"op": "create_split_screen", "slot_indexes": [0], "panel_count": 2, "duration_s": 3},
    )
    section = payload["split_screen_sections"][0]["id"]
    variant["speaker_background"] = {
        "kind": "color",
        "color": "#000000",
        "windows": [
            {"media_id": "short", "kind": "image", "start_s": 1.5, "end_s": 2.5},
            {"media_id": "survivor", "kind": "image", "start_s": 0, "end_s": 1},
        ],
        "full_screen_windows": [{"start_s": 1.5, "end_s": 2.5}, {"start_s": 0, "end_s": 1}],
    }
    result, _ = apply(
        job, variant, {"op": "update_split_screen", "section_id": section, "duration_s": 1.8}
    )
    background = result["speaker_background"]
    assert [r["media_id"] for r in background["windows"]] == ["survivor"]
    assert len(background["full_screen_windows"]) == 1
    assert background["full_screen_windows"][0]["end_s"] == 1
