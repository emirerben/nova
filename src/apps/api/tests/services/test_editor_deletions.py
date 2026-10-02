import pytest

from app.services.editor_deletions import (
    EditorDeletion,
    EditorDeletionError,
    apply_editor_deletions,
    canonical_caption_rows,
)


def test_explicit_text_and_music_removal_materializes_missing_lane() -> None:
    result = apply_editor_deletions(
        {"music_track_id": "track-1", "text_elements": [{"id": "generated-title"}]},
        deletions=[EditorDeletion("text", "generated-title"), EditorDeletion("music", "track-1")],
        sections={"text_elements": None},
    )

    assert result.sections["text_elements"] == []
    assert result.sections["remove_music"] is True


def test_stale_target_rejects_the_whole_deletion_set() -> None:
    with pytest.raises(EditorDeletionError):
        apply_editor_deletions(
            {"text_elements": [{"id": "still-here"}]},
            deletions=[EditorDeletion("text", "still-here"), EditorDeletion("text", "gone")],
            sections={},
        )


def test_clip_removal_preserves_slot_identity_and_marks_removed() -> None:
    result = apply_editor_deletions(
        {},
        deletions=[EditorDeletion("clip", "slot-2")],
        sections={},
        baseline_sections={
            "timeline_slots": [
                {"slot_id": "slot-1", "removed": False},
                {"slot_id": "slot-2", "removed": False},
            ]
        },
    )

    assert result.sections["timeline_slots"] == [
        {"slot_id": "slot-1", "removed": False},
        {"slot_id": "slot-2", "removed": True},
    ]


@pytest.mark.parametrize(
    ("kind", "lane"),
    [
        ("text", "text_elements"),
        ("caption_cue", "caption_cues"),
        ("sound_effect", "sound_effects"),
        ("media_overlay", "media_overlays"),
        ("visual_block", "visual_blocks"),
        ("motion_scene", "motion_scenes"),
        ("camera_effect", "camera_effects"),
    ],
)
def test_each_lane_deletion_materializes_an_empty_replacement(kind: str, lane: str) -> None:
    result = apply_editor_deletions(
        {lane: [{"id": "generated-or-protected"}]},
        deletions=[EditorDeletion(kind, "generated-or-protected")],
        sections={lane: None},
    )

    assert result.sections[lane] == []


def test_unknown_addition_is_not_treated_as_a_deletion() -> None:
    with pytest.raises(EditorDeletionError, match="stale"):
        apply_editor_deletions(
            {"media_overlays": [{"id": "owned"}]},
            deletions=[EditorDeletion("media_overlay", "forged")],
            sections={"media_overlays": [{"id": "owned"}, {"id": "forged"}]},
        )


def test_legacy_caption_ids_match_the_native_decoder_and_persist_on_delete() -> None:
    rows = canonical_caption_rows(
        [
            {"text": "first", "start_s": 0, "end_s": 1},
            {"id": "native-caption-1", "text": "second", "start_s": 1, "end_s": 2},
            {"text": "third", "start_s": 2, "end_s": 3},
        ]
    )
    assert [row["id"] for row in rows] == [
        "native-caption-0",
        "native-caption-1",
        "native-caption-2",
    ]
    result = apply_editor_deletions(
        {},
        deletions=[EditorDeletion("caption_cue", "native-caption-0")],
        sections={"caption_cues": None},
        baseline_sections={"caption_cues": rows},
    )
    assert result.sections["caption_cues"] == [
        {"id": "native-caption-1", "text": "second", "start_s": 1, "end_s": 2},
        {"id": "native-caption-2", "text": "third", "start_s": 2, "end_s": 3},
    ]


def test_lyric_deletions_accumulate_raw_line_keys() -> None:
    result = apply_editor_deletions(
        {
            "lyric_overlay_snapshot": [
                {"line_key": "verse-1", "text": "one"},
                {"line_key": "verse-2", "text": "two"},
            ]
        },
        deletions=[
            EditorDeletion("lyric_line", "lyric_verse-1"),
            EditorDeletion("lyric_line", "lyric_verse-2"),
        ],
        sections={},
    )
    assert result.sections["lyric_line_suppressions"] == ["verse-1", "verse-2"]


def test_text_delete_keeps_visual_block_and_clears_membership() -> None:
    result = apply_editor_deletions(
        {},
        deletions=[EditorDeletion("text", "title")],
        sections={},
        baseline_sections={
            "text_elements": [{"id": "title", "visual_block_id": "card"}],
            "visual_blocks": [{"id": "card", "kind": "media", "text_element_ids": ["title"]}],
        },
    )
    assert result.sections["text_elements"] == []
    assert result.sections["visual_blocks"] == [
        {"id": "card", "kind": "media", "text_element_ids": []}
    ]


@pytest.mark.parametrize("wire_id", ["L7", "lyric_L7"])
def test_native_lyric_identity_suppresses_the_absolute_line(wire_id: str) -> None:
    result = apply_editor_deletions(
        {"lyric_overlay_snapshot": [{"line_key": "L7", "text": "seven"}]},
        deletions=[EditorDeletion("lyric_line", wire_id)],
        sections={},
    )
    assert result.sections["lyric_line_suppressions"] == ["L7"]


def test_lyric_delete_without_snapshot_is_a_stale_target() -> None:
    with pytest.raises(EditorDeletionError, match="stale deletion target"):
        apply_editor_deletions({}, deletions=[EditorDeletion("lyric_line", "L7")], sections={})
