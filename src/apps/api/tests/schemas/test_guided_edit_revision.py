from __future__ import annotations

import pytest

from app.schemas.guided_edit_revision import (
    MAX_GUIDED_EDITOR_TEXT_ELEMENTS,
    GuidedEditorRevision,
    guided_editor_revision_from_approval,
    guided_editor_state_hash,
    normalize_guided_editor_revision,
)


def _revision(**overrides) -> dict:
    raw = {
        "approval_proposal_version": 4,
        "approval_media_digest": "a" * 64,
        "revision_number": 1,
        "sources": [
            {
                "media_id": "clip-1",
                "lane": "clip",
                "gcs_path": "users/u/plan/i/clip.mp4",
                "generation": "11",
                "kind": "video",
                "duration_s": 8.0,
            },
            {
                "media_id": "asset-1",
                "lane": "asset",
                "gcs_path": "users/u/plan/i/asset.jpg",
                "generation": "12",
                "kind": "image",
            },
        ],
        "segments": [
            {
                "segment_id": "seg-1",
                "media_id": "clip-1",
                "source_start_s": 1.0,
                "source_end_s": 4.0,
                "duration_s": 3.0,
                "output_start_s": 0.0,
                "output_end_s": 3.0,
            }
        ],
    }
    raw.update(overrides)
    return raw


def test_normalization_hashes_canonical_revision_and_keeps_unused_source() -> None:
    normalized = normalize_guided_editor_revision(_revision())
    assert normalized["state_hash"] == guided_editor_state_hash(normalized)
    assert [source["media_id"] for source in normalized["sources"]] == ["clip-1", "asset-1"]


def test_legacy_revision_hash_remains_valid_without_segment_layout() -> None:
    normalized = normalize_guided_editor_revision(_revision())

    # Frozen from the pre-layout schema. Adding an optional field must not
    # invalidate already-persisted revisions whose hash covered this payload.
    assert normalized["state_hash"] == (
        "d84ad87dd5ac7246c938bfe673d11510af6586a1675f09083b83d5c0e41c30ca"
    )
    assert "layout" not in normalized["segments"][0]
    assert GuidedEditorRevision.model_validate(normalized).segments[0].layout is None


def test_initial_revision_copies_layout_per_repeated_media_occurrence() -> None:
    revision = guided_editor_revision_from_approval(
        proposal_version=1,
        media_digest="a" * 64,
        snapshot={
            "media": [
                {
                    "media_id": "clip-1",
                    "lane": "clip",
                    "gcs_path": "users/u/clip.mp4",
                    "generation": "1",
                    "kind": "video",
                    "duration_s": 8.0,
                }
            ]
        },
        execution_plan={
            "story_timeline": [
                {
                    "moment_id": "one",
                    "media_id": "clip-1",
                    "source_start_s": 0.0,
                    "source_end_s": 2.0,
                    "duration_s": 2.0,
                    "output_start_s": 0.0,
                    "output_end_s": 2.0,
                    "layout": "supporting_card",
                },
                {
                    "moment_id": "two",
                    "media_id": "clip-1",
                    "source_start_s": 2.0,
                    "source_end_s": 4.0,
                    "duration_s": 2.0,
                    "output_start_s": 2.0,
                    "output_end_s": 4.0,
                    "layout": "fullscreen",
                },
            ],
            "transition_policy": {"type": "none", "duration_s": 0.0},
            "text_elements": [],
            "output_orientation": "portrait",
        },
    )

    assert [segment["layout"] for segment in revision["segments"]] == [
        "supporting_card",
        "fullscreen",
    ]


def test_guided_caption_lane_is_bounded_without_the_authored_text_ceiling() -> None:
    elements = [
        {"id": f"narration-caption-{index}", "text": "word", "start_s": 0, "end_s": 1}
        for index in range(MAX_GUIDED_EDITOR_TEXT_ELEMENTS + 1)
    ]
    revision = normalize_guided_editor_revision(_revision(text_elements=elements[:-1]))
    assert revision["text_elements"] == elements[:-1]
    with pytest.raises(ValueError, match="at most 5000 items"):
        normalize_guided_editor_revision(_revision(text_elements=elements))


def test_caption_deletion_history_has_room_without_expanding_other_lane_history() -> None:
    def deleted(lane, count):
        return [
            {"lane": lane, "record_id": f"deleted-{index}", "record": {"id": f"deleted-{index}"}}
            for index in range(count)
        ]

    assert (
        len(
            normalize_guided_editor_revision(_revision(tombstones=deleted("text_elements", 300)))[
                "tombstones"
            ]
        )
        == 300
    )
    with pytest.raises(ValueError, match="non-text history exceeds 200"):
        normalize_guided_editor_revision(_revision(tombstones=deleted("sound_effects", 201)))


def test_revision_rejects_source_outside_approved_pool() -> None:
    with pytest.raises(ValueError, match="unapproved source"):
        GuidedEditorRevision.model_validate(
            _revision(
                segments=[
                    {
                        "segment_id": "x",
                        "media_id": "new",
                        "duration_s": 1.0,
                        "output_start_s": 0.0,
                        "output_end_s": 1.0,
                    }
                ]
            )
        )


def test_revision_cas_rejects_stale_approval() -> None:
    with pytest.raises(ValueError, match="approval version is stale"):
        normalize_guided_editor_revision(_revision(), expected_approval_version=5)


def test_revision_rejects_source_window_past_pinned_generation_duration() -> None:
    with pytest.raises(ValueError, match="source window exceeds"):
        GuidedEditorRevision.model_validate(
            _revision(
                segments=[
                    {
                        "segment_id": "x",
                        "media_id": "clip-1",
                        "source_start_s": 7.0,
                        "source_end_s": 9.0,
                        "duration_s": 2.0,
                        "output_start_s": 0.0,
                        "output_end_s": 2.0,
                    }
                ]
            )
        )


def test_retime_and_normalized_crop_are_bounded_and_derive_source_window() -> None:
    raw = _revision(
        segments=[
            {
                "segment_id": "retimed",
                "media_id": "clip-1",
                "source_start_s": 1.0,
                "duration_s": 3.0,
                "playback_rate": 2.0,
                "source_crop": {"x": 0.1, "y": 0.2, "width": 0.6, "height": 0.7},
            }
        ]
    )
    normalized = normalize_guided_editor_revision(raw)
    segment = normalized["segments"][0]
    assert segment["source_end_s"] == 7.0
    assert segment["playback_rate"] == 2.0
    assert segment["source_crop"] == {"x": 0.1, "y": 0.2, "width": 0.6, "height": 0.7}


def test_short_retimed_source_shortens_and_reflows_later_segments() -> None:
    raw = _revision(
        segments=[
            {
                "segment_id": "first",
                "media_id": "clip-1",
                "source_start_s": 7.0,
                "duration_s": 2.0,
                "playback_rate": 2.0,
            },
            {
                "segment_id": "second",
                "media_id": "asset-1",
                "duration_s": 1.0,
            },
        ]
    )
    normalized = normalize_guided_editor_revision(raw)
    first, second = normalized["segments"]
    assert first["duration_s"] == 0.5
    assert first["source_end_s"] == 8.0
    assert second["output_start_s"] == 0.5


def test_legacy_implicit_slow_motion_keeps_its_authored_duration_and_hash() -> None:
    """Pre-rate revisions store their slow factor in source span only."""
    raw = _revision(
        sources=[{**_revision()["sources"][0], "duration_s": 2.0}],
        segments=[
            {
                "segment_id": "legacy-slow",
                "media_id": "clip-1",
                "source_start_s": 0.0,
                "source_end_s": 1.95,
                "duration_s": 5.0,
                "output_start_s": 0.0,
                "output_end_s": 5.0,
            }
        ],
    )
    normalized = normalize_guided_editor_revision(raw)
    assert normalized["segments"][0]["duration_s"] == 5.0
    # The pre-existing 30fps canonicalization remains stable; only the
    # duration clamp must not rewrite the legacy slow-motion meaning.
    assert normalized["segments"][0]["source_end_s"] == pytest.approx(1.933333)
    assert normalize_guided_editor_revision(normalized)["state_hash"] == normalized["state_hash"]


def test_explicit_retime_rejects_source_remainder_below_editor_floor() -> None:
    with pytest.raises(ValueError, match="source window is too short"):
        normalize_guided_editor_revision(
            _revision(
                sources=[{**_revision()["sources"][0], "duration_s": 2.0}],
                segments=[
                    {
                        "segment_id": "too-short-at-4x",
                        "media_id": "clip-1",
                        "source_start_s": 1.9,
                        "duration_s": 1.0,
                        "playback_rate": 4.0,
                    }
                ],
            )
        )


def test_caption_meta_round_trips_147_pinned_caption_records_without_rewriting_them() -> None:
    captions = [
        {
            "id": f"narration-caption-{index}",
            "text": "caption",
            "start_s": index / 10,
            "end_s": index / 10 + 0.1,
            "source_params": {"source": "caption_cue", "identity": f"cue-{index}"},
        }
        for index in range(147)
    ]
    revision = normalize_guided_editor_revision(
        _revision(
            text_elements=captions,
            caption_meta={"enabled": False, "style": "sentence", "size_px": 72},
        )
    )
    assert len(revision["text_elements"]) == 147
    assert revision["text_elements"][146]["source_params"]["source"] == "caption_cue"
    assert revision["caption_meta"] == {"enabled": False, "style": "sentence", "size_px": 72}


def test_revision_accepts_120_segments_and_rejects_121() -> None:
    sources = [
        {
            "media_id": f"image-{index}",
            "lane": "asset",
            "gcs_path": f"users/u/plan/i/image-{index}.jpg",
            "generation": "1",
            "kind": "image",
        }
        for index in range(121)
    ]
    segments = [
        {
            "segment_id": f"segment-{index}",
            "media_id": f"image-{index}",
            "duration_s": 0.1,
            "output_start_s": index / 10,
            "output_end_s": (index + 1) / 10,
        }
        for index in range(121)
    ]

    assert (
        len(
            GuidedEditorRevision.model_validate(
                _revision(sources=sources, segments=segments[:120])
            ).segments
        )
        == 120
    )
    with pytest.raises(ValueError, match="at most 120 items"):
        GuidedEditorRevision.model_validate(_revision(sources=sources, segments=segments))


def test_music_window_preserves_track_offset_and_clamps_length_to_story() -> None:
    normalized = normalize_guided_editor_revision(
        _revision(
            audio={
                "mode": "track",
                "track_id": "track-1",
                "title": "Track",
                "audio_gcs_path": "music/track-1.m4a",
                "generation": "77",
                "start_s": 4.0,
                "end_s": 20.0,
                "level": 0.4,
            }
        )
    )

    assert normalized["audio"]["start_s"] == 4.0
    assert normalized["audio"]["end_s"] == 7.0
    assert normalized["audio"]["level"] == 0.4


@pytest.mark.parametrize(
    ("transition_type", "duration_s", "expected", "expected_duration"),
    [
        ("none", 0.12, "cut", 0.0),
        ("crossfade", 0.12, "crossfade", 0.12),
    ],
)
def test_initial_revision_preserves_approved_transition_policy(
    transition_type: str,
    duration_s: float,
    expected: str,
    expected_duration: float,
) -> None:
    revision = guided_editor_revision_from_approval(
        proposal_version=1,
        media_digest="a" * 64,
        snapshot={
            "media": [
                {
                    "media_id": "clip-1",
                    "lane": "clip",
                    "gcs_path": "users/u/clip.mp4",
                    "generation": "1",
                    "kind": "video",
                    "duration_s": 8.0,
                }
            ]
        },
        execution_plan={
            "story_timeline": [
                {
                    "moment_id": "one",
                    "media_id": "clip-1",
                    "source_start_s": 0.0,
                    "source_end_s": 2.0,
                    "duration_s": 2.0,
                    "output_start_s": 0.0,
                    "output_end_s": 2.0,
                },
                {
                    "moment_id": "two",
                    "media_id": "clip-1",
                    "source_start_s": 2.0,
                    "source_end_s": 4.0,
                    "duration_s": 2.0,
                    "output_start_s": 2.0 - (duration_s if transition_type == "crossfade" else 0),
                    "output_end_s": 4.0 - (duration_s if transition_type == "crossfade" else 0),
                },
            ],
            "transition_policy": {"type": transition_type, "duration_s": duration_s},
            "text_elements": [],
            "output_orientation": "portrait",
        },
    )

    assert revision["segments"][0]["transition_after"] == expected
    assert revision["segments"][0]["transition_duration_s"] == expected_duration


def test_initial_revision_exposes_narration_labels_in_one_deduplicated_text_union() -> None:
    shared_label = {
        "id": "label-player-1",
        "text": "PLAYER 1",
        "start_s": 0.5,
        "end_s": 1.5,
        "role": "generative_sequence",
        "position": "custom",
        "source_params": {"narration_label_kind": "participant"},
    }
    revision = guided_editor_revision_from_approval(
        proposal_version=1,
        media_digest="a" * 64,
        snapshot={
            "media": [
                {
                    "media_id": "clip-1",
                    "lane": "clip",
                    "gcs_path": "users/u/clip.mp4",
                    "generation": "1",
                    "kind": "video",
                    "duration_s": 8.0,
                }
            ]
        },
        execution_plan={
            "story_timeline": [
                {
                    "moment_id": "one",
                    "media_id": "clip-1",
                    "source_start_s": 0.0,
                    "source_end_s": 2.0,
                    "duration_s": 2.0,
                    "output_start_s": 0.0,
                    "output_end_s": 2.0,
                }
            ],
            "transition_policy": {"type": "none", "duration_s": 0.0},
            "text_elements": [shared_label],
            "narration_label_text_elements": [shared_label],
            "output_orientation": "portrait",
        },
    )

    assert [row["id"] for row in revision["text_elements"]] == ["label-player-1"]


def test_a_revision_without_a_song_edit_keeps_its_exact_hash() -> None:
    normalized = normalize_guided_editor_revision(_revision())
    assert "user_song" not in normalized
    assert GuidedEditorRevision.model_validate(normalized).user_song is None
    # The hash is over a body with no `user_song` key at all, as before KRI-428.
    assert normalized["state_hash"] == guided_editor_state_hash(normalized)
    assert "user_song" not in GuidedEditorRevision.model_validate(normalized).model_dump(
        mode="json", exclude_none=False
    )


def test_a_creator_song_edit_is_hashed_validated_and_round_trips() -> None:
    plain = normalize_guided_editor_revision(_revision())
    edited = normalize_guided_editor_revision(
        _revision(user_song={"volume": 0.4, "window_start_s": 12.5})
    )
    assert edited["user_song"] == {"volume": 0.4, "window_start_s": 12.5, "removed": False}
    assert edited["state_hash"] != plain["state_hash"]
    again = normalize_guided_editor_revision(edited)
    assert again == edited
    for bad in (
        {"volume": 1.5},
        {"window_start_s": -1},
        {"window_start_s": float("inf")},
        {"level": 1},
    ):
        with pytest.raises(ValueError):
            normalize_guided_editor_revision(_revision(user_song=bad))


# ---- KRI-589: the drawings lane must not move any existing revision's hash -------------


def _rich_revision() -> dict:
    return _revision(
        text_elements=[{"id": "t1", "text": "Hi", "start_s": 0.0, "end_s": 1.5}],
        sound_effects=[{"id": "s1", "at_s": 0.5, "sound_effect_id": "pop"}],
        caption_meta={"style": "word"},
    )


def test_a_revision_without_drawings_hashes_exactly_as_before_the_lane_existed() -> None:
    """Frozen on the pre-drawings schema. In-flight render receipts pin ``state_hash`` and
    ``lane_hashes``, so the new lane has to be absent from both while it is empty."""
    for explicit in ({}, {"drawings": []}):
        normalized = normalize_guided_editor_revision({**_rich_revision(), **explicit})
        assert normalized["state_hash"] == (
            "044e9701af268c8a4e3e700923f2fdf1ec820ae00995860fb481c9933a6b104b"
        )
        assert "drawings" not in normalized
        assert "drawings" not in normalized["lane_hashes"]
        # ...and a revision stored before the lane existed still validates and re-hashes.
        assert (
            GuidedEditorRevision.model_validate(normalized).state_hash == normalized["state_hash"]
        )


def test_a_revision_with_a_drawing_hashes_it_and_round_trips() -> None:
    drawing = {
        "id": "d1",
        "start_s": 0.0,
        "end_s": 2.0,
        "canvas_aspect": 0.5625,
        "strokes": [{"color": "#FF3B30", "width_frac": 0.01, "points": [[0.1, 0.1], [0.2, 0.2]]}],
    }
    normalized = normalize_guided_editor_revision({**_rich_revision(), "drawings": [drawing]})
    assert normalized["drawings"] == [drawing]
    assert "drawings" in normalized["lane_hashes"]
    assert normalized["state_hash"] != (
        "044e9701af268c8a4e3e700923f2fdf1ec820ae00995860fb481c9933a6b104b"
    )
    assert GuidedEditorRevision.model_validate(normalized).drawings == [drawing]
    # A drawing id is part of the revision's cross-lane identity namespace.
    with pytest.raises(ValueError, match="unique across lanes"):
        normalize_guided_editor_revision(
            {**_rich_revision(), "drawings": [{**drawing, "id": "t1"}]}
        )


# ---- KRI-1117: the speaker-background lane must not move any existing revision's hash --------


def test_a_revision_without_a_background_hashes_exactly_as_before_the_lane_existed() -> None:
    for explicit in ({}, {"speaker_background": None}):
        normalized = normalize_guided_editor_revision({**_rich_revision(), **explicit})
        assert normalized["state_hash"] == (
            "044e9701af268c8a4e3e700923f2fdf1ec820ae00995860fb481c9933a6b104b"
        )
        assert "speaker_background" not in normalized
        assert "speaker_background" not in normalized["lane_hashes"]
        assert (
            GuidedEditorRevision.model_validate(normalized).state_hash == normalized["state_hash"]
        )


def test_a_revision_with_a_background_hashes_it_and_round_trips() -> None:
    background = {"media_id": "visual-1", "kind": "video", "source_start_s": 1.5}
    normalized = normalize_guided_editor_revision(
        {**_rich_revision(), "speaker_background": background}
    )
    assert normalized["speaker_background"] == background
    assert "speaker_background" in normalized["lane_hashes"]
    assert normalized["state_hash"] != (
        "044e9701af268c8a4e3e700923f2fdf1ec820ae00995860fb481c9933a6b104b"
    )
    assert GuidedEditorRevision.model_validate(normalized).speaker_background == background
    other = normalize_guided_editor_revision(
        {**_rich_revision(), "speaker_background": {**background, "source_start_s": 2.0}}
    )
    assert other["state_hash"] != normalized["state_hash"]
