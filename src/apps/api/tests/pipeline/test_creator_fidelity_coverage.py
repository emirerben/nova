"""Structural replay shape: 19 videos, 20 photos and a continuous 44.7s narration."""

import pytest
from pydantic import ValidationError

from app.pipeline.guided_story import compile_execution_plan
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MixedMediaTimingProfile,
    NarrationTrack,
    StoryBeat,
    canonical_media_digest,
    canonical_narration_duration_s,
)


def original_input_shape():
    media = [
        MediaRef(
            lane="clip",
            media_id=f"video-{i}",
            gcs_path=f"users/u/{i}.mp4",
            generation="1",
            kind="video",
            duration_s=8,
        )
        for i in range(19)
    ]
    media += [
        MediaRef(
            lane="asset",
            media_id=f"photo-{i}",
            gcs_path=f"users/u/{i}.jpg",
            generation="1",
            kind="image",
        )
        for i in range(20)
    ]
    cuts = []
    for index, ref in enumerate(media):
        # 1,341 total frames minus 180 photo frames leaves 1,161 video frames.
        duration = 0.3 if ref.kind == "image" else (62 if index < 2 else 61) / 30
        cuts.append(
            FastMontageCut(
                cut_id=f"cut-{index}",
                media_id=ref.media_id,
                source_start_s=0,
                source_end_s=duration,
                output_duration_s=duration,
                role="hook" if index == 0 else "build",
                transition="none",
            )
        )
    return EditProposalSnapshot(
        direction="fast_montage",
        goal="Use every video and photo with the recording",
        pace="fast",
        duration_s=45,
        title="Sports day",
        media=media,
        media_scope="all",
        fast_cuts=cuts,
        mixed_media_timing=MixedMediaTimingProfile(
            image_hold="very_fast",
            image_hold_s=0.3,
            video_hold="longer",
            boundary_style="cut",
            image_grouping="runs",
        ),
        narration=NarrationTrack(
            gcs_path="voiceover-uploads/take.mp3", generation="7", duration_s=44.7
        ),
        story_beats=[
            StoryBeat(
                beat_id=f"beat-{i}",
                topic=f"Part {i}",
                media_ids=[ref.media_id for ref in media[i : i + 4]],
                duration_s=4.5,
            )
            for i in range(0, len(media), 4)
        ],
    )


def compile_snapshot(snapshot):
    return compile_execution_plan(
        {
            "proposal_version": 1,
            "media_digest": canonical_media_digest(snapshot.media, snapshot.narration),
            "approved_proposal": snapshot.model_dump(mode="json"),
            "media_identities": [
                ref.model_dump(include={"lane", "media_id", "gcs_path", "generation", "kind"})
                for ref in snapshot.media
            ],
        },
        track=None,
    )


def test_all_39_sources_reach_narration_timeline_with_nine_frame_photos():
    snapshot = original_input_shape()
    plan = compile_snapshot(snapshot)
    assert set(plan["selected_media_ids"]) == {ref.media_id for ref in snapshot.media}
    timeline = plan["story_timeline"]
    assert len(timeline) == 39
    photos = [moment for moment in timeline if moment["kind"] == "image"]
    assert len(photos) == 20
    assert all(round(moment["duration_s"] * 30) == 9 for moment in photos)
    assert sum(moment["duration_s"] for moment in timeline) == pytest.approx(44.7, abs=0.001)
    assert plan["resolved_duration_s"] == 44.7
    assert plan["music"] is None
    assert plan["narration"]["generation"] == "7"


def test_all_scope_cannot_silently_omit_an_input():
    payload = original_input_shape().model_dump(mode="json")
    payload["fast_cuts"].pop()
    with pytest.raises((ValidationError, ValueError)):
        compile_snapshot(EditProposalSnapshot.model_validate(payload))


@pytest.mark.parametrize(
    ("audio_s", "visual_s"),
    [(44.688, 44.7), (44.71, 44.733333)],
)
def test_narration_visual_budget_is_frame_ceiling(audio_s, visual_s):
    assert canonical_narration_duration_s(audio_s) == pytest.approx(visual_s, abs=1e-6)
    assert canonical_narration_duration_s(audio_s) >= audio_s


def test_fractional_pinned_audio_compiles_to_the_next_visual_frame():
    snapshot = original_input_shape()
    snapshot.narration.duration_s = 44.688

    plan = compile_snapshot(snapshot)

    assert plan["resolved_duration_s"] == pytest.approx(44.7)
    assert plan["resolved_duration_s"] >= snapshot.narration.duration_s
    assert all(
        round(moment["duration_s"] * 30) == 9
        for moment in plan["story_timeline"]
        if moment["kind"] == "image"
    )


def test_pinned_audio_above_the_previous_frame_never_shortens_the_visual_plan():
    snapshot = original_input_shape()
    snapshot.narration.duration_s = 44.71
    video_cuts = [cut for cut in snapshot.fast_cuts or [] if cut.media_id.startswith("video-")]
    frame_aligned_cuts = []
    video_index = 0
    for cut in snapshot.fast_cuts or []:
        if cut.media_id.startswith("video-"):
            frames = 62 if video_index < 3 else 61
            video_index += 1
            duration_s = frames / 30
            frame_aligned_cuts.append(
                cut.model_copy(
                    update={
                        "source_end_s": duration_s,
                        "output_duration_s": duration_s,
                    }
                )
            )
        else:
            frame_aligned_cuts.append(cut)
    assert len(video_cuts) == 19
    snapshot.fast_cuts = frame_aligned_cuts

    plan = compile_snapshot(snapshot)

    assert plan["resolved_duration_s"] == pytest.approx(44.733333, abs=0.001)
    assert plan["resolved_duration_s"] >= snapshot.narration.duration_s


def test_narration_can_hold_long_video_without_repeating_sources():
    payload = original_input_shape().model_dump(mode="json")
    for index, cut in enumerate(payload["fast_cuts"][:19]):
        frames = 150 if index == 0 else 57 if index <= 3 else 56
        cut["source_end_s"] = frames / 30
        cut["output_duration_s"] = frames / 30
    snapshot = EditProposalSnapshot.model_validate(payload)
    plan = compile_snapshot(snapshot)
    assert plan["story_timeline"][0]["duration_s"] == 5
    assert len(plan["story_timeline"]) == 39
    assert plan["resolved_duration_s"] == pytest.approx(44.7)
    payload["narration"] = None
    with pytest.raises(ValidationError, match="mixed-media videos"):
        EditProposalSnapshot.model_validate(payload)


def test_all_media_selection_does_not_force_upload_order():
    payload = original_input_shape().model_dump(mode="json")
    payload["selected_media_ids"] = [ref["media_id"] for ref in payload["media"]]
    cuts = payload["fast_cuts"]
    payload["fast_cuts"] = cuts[19:] + cuts[:19]
    for index, cut in enumerate(payload["fast_cuts"]):
        cut["role"] = "hook" if index == 0 else "payoff" if index == 38 else "build"
    plan = compile_snapshot(EditProposalSnapshot.model_validate(payload))
    assert plan["selected_media_ids"][0] == "photo-0"
    assert set(plan["selected_media_ids"]) == set(payload["selected_media_ids"])
    assert [row["media_id"] for row in plan["story_timeline"]] == plan["selected_media_ids"]
