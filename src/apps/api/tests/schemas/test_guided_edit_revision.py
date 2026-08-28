from __future__ import annotations

import pytest

from app.schemas.guided_edit_revision import (
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
