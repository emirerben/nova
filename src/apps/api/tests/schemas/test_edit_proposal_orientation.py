"""Rotation-aware orientation voting (autoplace ANALYSIS_VERSION 6).

A phone recorded in portrait but stored with a Display Matrix rotation flag
(e.g. -90) reports CODED pixel dims as landscape (1920x1080, aspect 1.78)
even though it displays as portrait. `_media_aspect` must prefer
display_width/display_height (rotation-aware) over `ref.aspect` / analysis
width-height (both CODED pixels), while staying byte-identical for legacy
rows that never persisted display dims.
"""

from __future__ import annotations

from app.schemas.edit_proposal import EditProposalSnapshot, MediaRef, StoryBeat, _media_aspect


def _snapshot_with_media(media: list[MediaRef]) -> EditProposalSnapshot:
    return EditProposalSnapshot(
        duration_s=10,
        title="Test",
        media=media,
        story_beats=[
            StoryBeat(
                beat_id=f"beat-{i}",
                topic=f"Topic {i}",
                thought=f"Thought {i}",
                media_ids=[ref.media_id],
                duration_s=10,
            )
            for i, ref in enumerate(media, start=1)
        ],
    )


def test_rotated_portrait_clip_votes_portrait_from_display_dims() -> None:
    ref = MediaRef(
        lane="clip",
        media_id="rotated-clip",
        gcs_path="users/u/rotated.mp4",
        generation="1",
        kind="video",
        duration_s=10,
        aspect=1.7778,
        analysis={
            "width": 1920,
            "height": 1080,
            "display_width": 1080,
            "display_height": 1920,
            "rotation_degrees": -90,
        },
    )

    aspect = _media_aspect(ref)
    assert aspect is not None
    assert aspect < 1  # portrait, not the CODED-pixel 1.78 landscape

    snapshot = _snapshot_with_media([ref])
    assert snapshot.output_orientation == "portrait"


def test_legacy_analysis_without_display_dims_keeps_todays_vote() -> None:
    ref = MediaRef(
        lane="clip",
        media_id="legacy-clip",
        gcs_path="users/u/legacy.mp4",
        generation="1",
        kind="video",
        duration_s=10,
        aspect=1.7778,
        analysis={"width": 1920, "height": 1080},
    )

    aspect = _media_aspect(ref)
    assert aspect == 1.7778  # unchanged backward-compat pin

    snapshot = _snapshot_with_media([ref])
    assert snapshot.output_orientation == "landscape"


def test_media_aspect_ignores_malformed_display_dims() -> None:
    ref = MediaRef(
        lane="clip",
        media_id="bad-clip",
        gcs_path="users/u/bad.mp4",
        generation="1",
        kind="video",
        duration_s=10,
        aspect=1.7778,
        analysis={"display_width": "oops", "display_height": 1920},
    )

    aspect = _media_aspect(ref)
    assert aspect == 1.7778  # falls through cleanly to ref.aspect, no raise
