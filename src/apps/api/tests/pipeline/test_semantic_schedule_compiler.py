"""KRI-133: compiler v8 projects approved frames without reallocation."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.pipeline.guided_story import (
    GuidedStoryError,
    _compile_execution_plan_version,
    compile_execution_plan,
)
from app.schemas.edit_frame_schedule import EditFrameSchedule, FrameScheduledMoment
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MontageAudioPlan,
    MontageTextBinding,
    NarrationTrack,
    StoryBeat,
    canonical_media_digest,
)


def _media() -> list[MediaRef]:
    return [
        MediaRef(
            lane="asset", media_id="photo-a", gcs_path="users/a.jpg", generation="1", kind="image"
        ),
        MediaRef(
            lane="asset", media_id="photo-b", gcs_path="users/b.jpg", generation="2", kind="image"
        ),
        MediaRef(
            lane="clip",
            media_id="clip-c",
            gcs_path="users/c.mp4",
            generation="3",
            kind="video",
            duration_s=12,
        ),
    ]


def _schedule(*, direction: str = "guided_story", transition_frames: int = 4) -> EditFrameSchedule:
    # Three exact source/output windows. The two four-frame crossfades make
    # 184 + 184 + 180 - 8 = 540 frames (18 seconds) exactly.
    return EditFrameSchedule(
        total_frames=540,
        transition_frames=transition_frames,
        direction=direction,
        moments=[
            FrameScheduledMoment(
                moment_id="beat-a",
                beat_id="beat-a",
                media_id="photo-a",
                source_start_frame=0,
                source_end_frame=184,
                output_start_frame=0,
                output_end_frame=184,
                layout="supporting_card",
                role="hook",
            ),
            FrameScheduledMoment(
                moment_id="beat-b",
                beat_id="beat-b",
                media_id="photo-b",
                source_start_frame=0,
                source_end_frame=184,
                output_start_frame=180,
                output_end_frame=364,
                layout="supporting_card",
                role="build",
            ),
            FrameScheduledMoment(
                moment_id="beat-c",
                beat_id="beat-c",
                media_id="clip-c",
                source_start_frame=60,
                source_end_frame=240,
                output_start_frame=360,
                output_end_frame=540,
                layout="fullscreen",
                role="payoff",
            ),
        ],
    )


def _snapshot(*, schedule: EditFrameSchedule | None = None) -> EditProposalSnapshot:
    media = _media()
    return EditProposalSnapshot(
        frame_schedule=schedule,
        direction="guided_story",
        goal="A day by the coast",
        pace="balanced",
        duration_s=18,
        title="Coast day",
        media=media,
        story_beats=[
            StoryBeat(
                beat_id="beat-a",
                topic="Arrival",
                media_ids=["photo-a"],
                layout="supporting_card",
                duration_s=6,
            ),
            StoryBeat(
                beat_id="beat-b",
                topic="Walk",
                media_ids=["photo-b"],
                layout="supporting_card",
                duration_s=6,
            ),
            StoryBeat(
                beat_id="beat-c",
                topic="Water",
                media_ids=["clip-c"],
                layout="fullscreen",
                duration_s=6,
            ),
        ],
    )


def _raw(snapshot: EditProposalSnapshot) -> dict:
    return {
        "proposal_version": 9,
        "media_digest": canonical_media_digest(snapshot.media, snapshot.narration),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {
                "lane": ref.lane,
                "media_id": ref.media_id,
                "gcs_path": ref.gcs_path,
                "generation": ref.generation,
                "kind": ref.kind,
            }
            for ref in snapshot.media
        ],
    }


def _legacy_baseline_raw() -> dict:
    """Exact representative snapshot hashed from pristine a90d5fe1e."""
    media = [
        MediaRef(
            lane="clip",
            media_id="coast-video",
            gcs_path="users/u/coast.mp4",
            generation="11",
            kind="video",
            duration_s=12,
            analysis={
                "subject": "coast",
                "description": "turquoise sea and a small boat",
                "best_moments": [{"start_s": 2, "end_s": 10, "description": "boat"}],
            },
        ),
        MediaRef(
            lane="asset",
            media_id="food-photo",
            gcs_path="users/u/food.jpg",
            generation="12",
            kind="image",
            analysis={"subject": "food", "description": "ice cream"},
        ),
        MediaRef(
            lane="asset",
            media_id="town-photo",
            gcs_path="users/u/town.jpg",
            generation="13",
            kind="image",
            analysis={"subject": "architecture", "description": "old town street"},
        ),
    ]
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        goal="Explain what stood out in Corfu",
        pace="balanced",
        duration_s=18,
        title="What Corfu felt like",
        media=media,
        story_beats=[
            StoryBeat(
                beat_id="food",
                topic="Food",
                thought="Small treats made the hot afternoons better.",
                media_ids=["food-photo"],
                layout="supporting_card",
                duration_s=4,
            ),
            StoryBeat(
                beat_id="town",
                topic="Architecture",
                thought="The old streets reward slow wandering.",
                media_ids=["town-photo"],
                layout="supporting_card",
                duration_s=4,
            ),
            StoryBeat(
                beat_id="coast",
                topic="Coast",
                thought="The water changes the pace of the whole day.",
                media_ids=["coast-video"],
                layout="supporting_card",
                duration_s=4,
            ),
        ],
    )
    return _raw(snapshot) | {"proposal_version": 7}


def test_v8_projects_exact_approved_frames_and_does_not_snap_to_track_beats() -> None:
    plan = compile_execution_plan(
        _raw(_snapshot(schedule=_schedule())),
        track={
            "track_id": "song",
            "title": "Song",
            "artist": "Artist",
            "catalog_duration_s": 60,
            "start_s": 0,
            "beat_timestamps_s": [0.1, 1.2, 6.4],
        },
    )

    assert plan["compiler_version"] == 8
    assert plan["resolved_duration_s"] == 18.0
    assert plan["transition_policy"] == {"type": "crossfade", "duration_s": 4 / 30}
    assert [
        (row["source_start_s"], row["source_end_s"], row["output_start_s"], row["output_end_s"])
        for row in plan["story_timeline"]
    ] == [
        (0.0, 184 / 30, 0.0, 184 / 30),
        (0.0, 184 / 30, 6.0, 364 / 30),
        (2.0, 8.0, 12.0, 18.0),
    ]
    assert plan["story_timeline"][1]["output_start_s"] == 6.0  # not the 6.4s music beat


@pytest.mark.parametrize(
    "mutate",
    [
        lambda snapshot: snapshot.model_copy(update={"duration_s": 17}),
        lambda snapshot: snapshot.model_copy(update={"direction": "text_explainer"}),
        lambda snapshot: snapshot.model_copy(update={"media": snapshot.media[:-1]}),
        lambda snapshot: snapshot.model_copy(
            update={
                "video_reuse_policy": "once",
                "frame_schedule": EditFrameSchedule(
                    total_frames=540,
                    transition_frames=4,
                    direction="guided_story",
                    moments=[
                        FrameScheduledMoment(
                            moment_id="beat-a",
                            beat_id="beat-a",
                            media_id="clip-c",
                            source_start_frame=0,
                            source_end_frame=184,
                            output_start_frame=0,
                            output_end_frame=184,
                            role="hook",
                        ),
                        FrameScheduledMoment(
                            moment_id="beat-b",
                            beat_id="beat-b",
                            media_id="photo-b",
                            source_start_frame=0,
                            source_end_frame=184,
                            output_start_frame=180,
                            output_end_frame=364,
                            layout="supporting_card",
                            role="build",
                        ),
                        FrameScheduledMoment(
                            moment_id="beat-c",
                            beat_id="beat-c",
                            media_id="clip-c",
                            source_start_frame=180,
                            source_end_frame=360,
                            output_start_frame=360,
                            output_end_frame=540,
                            role="payoff",
                        ),
                    ],
                ),
            }
        ),
    ],
)
def test_v8_rejects_schedule_that_no_longer_matches_approved_contract(mutate) -> None:
    with pytest.raises(GuidedStoryError):
        compile_execution_plan(_raw(mutate(_snapshot(schedule=_schedule()))), track=None)


def test_v8_rejects_a_scheduled_source_window_beyond_uploaded_video() -> None:
    schedule = _schedule().model_copy(
        update={
            "moments": [
                *_schedule().moments[:2],
                FrameScheduledMoment(
                    moment_id="beat-c",
                    beat_id="beat-c",
                    media_id="clip-c",
                    source_start_frame=181,
                    source_end_frame=361,
                    output_start_frame=360,
                    output_end_frame=540,
                    role="payoff",
                ),
            ]
        }
    )
    with pytest.raises(GuidedStoryError, match="exceeds the uploaded video"):
        compile_execution_plan(_raw(_snapshot(schedule=schedule)), track=None)


def test_v8_preserves_fast_montage_audio_title_and_labels() -> None:
    snapshot = _snapshot(schedule=None)
    videos = [
        MediaRef(
            lane="clip",
            media_id=f"clip-{index}",
            gcs_path=f"users/{index}.mp4",
            generation=str(index),
            kind="video",
            duration_s=12,
        )
        for index in range(1, 4)
    ]
    snapshot = snapshot.model_copy(
        update={
            "direction": "fast_montage",
            "pace": "fast",
            "duration_s": 18,
            "media": videos,
            "story_beats": [
                StoryBeat(beat_id="cut-a", topic="A", media_ids=["clip-1"], duration_s=6),
                StoryBeat(beat_id="cut-b", topic="B", media_ids=["clip-2"], duration_s=6),
                StoryBeat(beat_id="cut-c", topic="C", media_ids=["clip-3"], duration_s=6),
            ],
            "fast_cuts": [
                FastMontageCut(
                    cut_id="cut-a",
                    media_id="clip-1",
                    source_start_s=0,
                    source_end_s=6,
                    output_duration_s=6,
                    role="hook",
                ),
                FastMontageCut(
                    cut_id="cut-b",
                    media_id="clip-2",
                    source_start_s=0,
                    source_end_s=6,
                    output_duration_s=6,
                    role="build",
                ),
                FastMontageCut(
                    cut_id="cut-c",
                    media_id="clip-3",
                    source_start_s=2,
                    source_end_s=8,
                    output_duration_s=6,
                    role="payoff",
                ),
            ],
            "frame_schedule": EditFrameSchedule(
                total_frames=540,
                transition_frames=0,
                direction="fast_montage",
                moments=[
                    FrameScheduledMoment(
                        moment_id="cut-a",
                        beat_id="cut-a",
                        media_id="clip-1",
                        source_start_frame=0,
                        source_end_frame=180,
                        output_start_frame=0,
                        output_end_frame=180,
                        role="hook",
                    ),
                    FrameScheduledMoment(
                        moment_id="cut-b",
                        beat_id="cut-b",
                        media_id="clip-2",
                        source_start_frame=0,
                        source_end_frame=180,
                        output_start_frame=180,
                        output_end_frame=360,
                        role="build",
                    ),
                    FrameScheduledMoment(
                        moment_id="cut-c",
                        beat_id="cut-c",
                        media_id="clip-3",
                        source_start_frame=60,
                        source_end_frame=240,
                        output_start_frame=360,
                        output_end_frame=540,
                        role="payoff",
                    ),
                ],
            ),
            "opening_title": "Three moments",
            "montage_text_bindings": [MontageTextBinding(media_id="clip-1", text="Arrive")],
            "montage_audio": MontageAudioPlan(
                preserve_source_audio=True, preview_source_beds=False, source_media_ids=[]
            ),
            "narration": NarrationTrack(
                gcs_path="users/voice.m4a", generation="voice-1", duration_s=18
            ),
        }
    )

    plan = compile_execution_plan(_raw(snapshot), track=None)
    assert plan["montage_audio"]["preserve_source_audio"] is True
    assert any(row["id"] == "guided-title" for row in plan["text_elements"])
    assert any(row["id"].startswith("montage-text-") for row in plan["text_elements"])


def test_legacy_v1_to_v7_replay_remains_deterministic_without_schedule() -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/pipeline/legacy_guided_story_v1_v7_hashes.json"
        ).read_text()
    )
    raw = _legacy_baseline_raw()
    for version in range(1, 8):
        first = _compile_execution_plan_version(
            copy.deepcopy(raw), track=None, compiler_version=version
        )
        replay = _compile_execution_plan_version(
            copy.deepcopy(raw), track=None, compiler_version=version
        )
        assert first == replay
        assert first["compiler_version"] == version
        digest = hashlib.sha256(
            json.dumps(first, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert digest == fixture["hashes"][str(version)]
