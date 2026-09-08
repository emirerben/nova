"""Narration remains authoritative when a specialist draft cannot be accepted."""

import pytest

from app.pipeline.guided_story import validate_proposal_timing
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    MediaRef,
    MixedMediaTimingProfile,
    NarrationTrack,
    StoryBeat,
)
from app.services import edit_direction_planner


def _mixed_media() -> list[MediaRef]:
    # Short phone clips plus one longer take: the footage fits a 44.7s
    # narration, but the legacy 3s per-window ceiling exhausts its allocator.
    lengths = [
        1.733333,
        13.305034,
        2.066667,
        0.433333,
        1.3,
        1.766667,
        1.9,
        2.233333,
        0.933333,
        0.598345,
        3.0,
        0.298345,
        2.466667,
        0.9,
        3.068345,
        1.915011,
        2.96,
        2.833333,
        2.3,
    ]
    return [
        MediaRef(
            lane="clip",
            media_id=f"clip-{i}",
            gcs_path=f"users/test/clip-{i}.mp4",
            generation="1",
            kind="video",
            duration_s=duration,
        )
        for i, duration in enumerate(lengths)
    ] + [
        MediaRef(
            lane="asset",
            media_id=f"image-{i}",
            gcs_path=f"users/test/image-{i}.jpg",
            generation="1",
            kind="image",
        )
        for i in range(20)
    ]


def _timing() -> MixedMediaTimingProfile:
    return MixedMediaTimingProfile(
        image_hold="very_fast",
        image_hold_s=0.3,
        video_hold="longer",
        boundary_style="cut",
        image_grouping="runs",
    )


def _narration() -> NarrationTrack:
    return NarrationTrack(gcs_path="voiceover/test.m4a", generation="1", duration_s=44.688)


def test_narrated_recovery_preserves_all_sources_photo_frames_and_full_audio() -> None:
    media = _mixed_media()
    cuts = edit_direction_planner.deterministic_fast_cuts(
        media,
        45,
        _timing(),
        narration_duration_s=44.688,
        required_media_ids=[ref.media_id for ref in media],
    )
    assert len(cuts) == len(media) == 39
    assert {cut.media_id for cut in cuts} == {ref.media_id for ref in media}
    assert sum(round(cut.output_duration_s * 30) for cut in cuts) == 1341
    by_id = {ref.media_id: ref for ref in media}
    photos = [cut for cut in cuts if by_id[cut.media_id].kind == "image"]
    assert len(photos) == 20
    assert all(cut.output_duration_s == 0.3 for cut in photos)
    assert any(cut.output_duration_s > 3 for cut in cuts)
    for index, cut in enumerate(cuts):
        ref = by_id[cut.media_id]
        assert cut.output_duration_s * 30 == pytest.approx(round(cut.output_duration_s * 30))
        if ref.kind == "video":
            assert cut.source_end_s <= ref.duration_s + 0.001
        else:
            assert any(
                0 <= adjacent < len(cuts) and by_id[cuts[adjacent].media_id].kind == "image"
                for adjacent in (index - 1, index + 1)
            )
    snapshot = EditProposalSnapshot(
        direction="fast_montage",
        goal="Use every source",
        pace="fast",
        duration_s=45,
        title="Match day",
        media=media,
        fast_cuts=cuts,
        story_beats=edit_direction_planner._compatibility_beats(cuts),
        narration=_narration(),
        mixed_media_timing=_timing(),
        media_scope="all",
        selected_media_ids=[ref.media_id for ref in media],
    )
    validate_proposal_timing(snapshot)


def test_narrated_recovery_does_not_shorten_audio_to_fit_insufficient_sources() -> None:
    with pytest.raises(ValueError, match="cannot allocate"):
        edit_direction_planner.deterministic_fast_cuts(
            _mixed_media(),
            60,
            _timing(),
            narration_duration_s=60,
            required_media_ids=[ref.media_id for ref in _mixed_media()],
        )


@pytest.mark.parametrize("required", [["unknown"], ["clip-0", "unusable"]])
def test_narrated_recovery_rejects_missing_or_unusable_required_media(required) -> None:
    media = _mixed_media() + [
        MediaRef(
            lane="clip",
            media_id="unusable",
            gcs_path="users/test/short.mp4",
            generation="1",
            kind="video",
            duration_s=0.01,
        )
    ]
    with pytest.raises(ValueError, match="required"):
        edit_direction_planner.deterministic_fast_cuts(
            media,
            45,
            _timing(),
            narration_duration_s=44.688,
            required_media_ids=required,
        )


def test_narrated_recovery_respects_explicit_selected_scope() -> None:
    media = _mixed_media()
    selected = ["clip-1", "image-0", "image-1"]
    cuts = edit_direction_planner.deterministic_fast_cuts(
        media,
        8,
        _timing(),
        narration_duration_s=7.99,
        required_media_ids=selected,
    )
    assert {cut.media_id for cut in cuts} == set(selected)
    assert sum(round(cut.output_duration_s * 30) for cut in cuts) == 240


def test_narrated_replan_uses_the_same_safe_recovery(monkeypatch) -> None:
    from app.agents._runtime import TerminalError

    source = EditProposalSnapshot(
        direction="fast_montage",
        goal="Use every source",
        pace="fast",
        duration_s=45,
        title="Match day",
        media=_mixed_media(),
        narration=_narration(),
        mixed_media_timing=_timing(),
        media_scope="all",
        story_beats=[
            StoryBeat(beat_id="old", topic="Original", media_ids=["clip-1"], duration_s=12)
        ],
    )
    monkeypatch.setattr(edit_direction_planner, "default_client", lambda: None)
    monkeypatch.setattr(
        edit_direction_planner.EditProposalAgent,
        "run",
        lambda *_a, **_kw: (_ for _ in ()).throw(TerminalError("invalid specialist draft")),
    )
    result = edit_direction_planner.plan_direction_snapshot(
        source,
        direction="fast_montage",
        goal=source.goal,
        pace="fast",
        duration_s=45,
        mixed_media_timing=_timing(),
    )
    assert result.narration == source.narration
    assert len(result.fast_cuts) == 39
    assert sum(round(c.output_duration_s * 30) for c in result.fast_cuts) == 1341
    validate_proposal_timing(result)
