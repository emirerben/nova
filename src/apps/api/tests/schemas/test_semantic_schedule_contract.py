"""KRI-133 schema boundary tests for the private frame schedule contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.edit_frame_schedule import EditFrameSchedule, FrameScheduledMoment
from app.schemas.edit_proposal import (
    EditProposal,
    EditProposalResponse,
    EditProposalSnapshot,
    MediaRef,
    StoryBeat,
)


def _moment(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "moment_id": "m1",
        "beat_id": "b1",
        "media_id": "clip-1",
        "source_start_frame": 0,
        "source_end_frame": 90,
        "output_start_frame": 0,
        "output_end_frame": 90,
        "role": "hook",
    }
    value.update(updates)
    return value


def _snapshot(**updates: object) -> EditProposalSnapshot:
    value: dict[str, object] = {
        "direction": "guided_story",
        "duration_s": 3,
        "title": "A clip",
        "media": [
            MediaRef(
                lane="clip",
                media_id="clip-1",
                gcs_path="users/clip.mp4",
                generation="1",
                kind="video",
                duration_s=3,
            )
        ],
        "story_beats": [StoryBeat(beat_id="b1", topic="Start", media_ids=["clip-1"], duration_s=3)],
    }
    value.update(updates)
    return EditProposalSnapshot(**value)


def test_schedule_requires_canonical_integer_frame_timeline() -> None:
    schedule = EditFrameSchedule(
        total_frames=90,
        transition_frames=0,
        direction="guided_story",
        moments=[FrameScheduledMoment(**_moment())],
    )
    assert schedule.model_dump(mode="json")["fps"] == 30

    with pytest.raises(ValidationError, match="equal duration"):
        FrameScheduledMoment(**_moment(source_end_frame=89))
    with pytest.raises(ValidationError, match="start at output frame zero"):
        EditFrameSchedule(
            total_frames=90,
            transition_frames=0,
            direction="guided_story",
            moments=[FrameScheduledMoment(**_moment(output_start_frame=1, output_end_frame=91))],
        )
    with pytest.raises(ValidationError, match="canonical transition overlap"):
        EditFrameSchedule(
            total_frames=177,
            transition_frames=3,
            direction="guided_story",
            moments=[
                FrameScheduledMoment(**_moment(output_end_frame=90)),
                FrameScheduledMoment(
                    **_moment(
                        moment_id="m2",
                        output_start_frame=88,
                        output_end_frame=177,
                        source_end_frame=89,
                        role="payoff",
                    )
                ),
            ],
        )


def test_absent_schedule_is_omitted_so_legacy_approval_bytes_stay_stable() -> None:
    legacy = _snapshot()
    dumped = legacy.model_dump(mode="json")
    assert "frame_schedule" not in dumped
    assert EditProposalSnapshot.model_validate(dumped).model_dump(mode="json") == dumped


def test_schedule_round_trips_as_an_immutable_snapshot_field() -> None:
    schedule = EditFrameSchedule(
        total_frames=90,
        transition_frames=0,
        direction="guided_story",
        moments=[FrameScheduledMoment(**_moment())],
    )
    snapshot = _snapshot(frame_schedule=schedule)
    dumped = snapshot.model_dump(mode="json")
    assert dumped["frame_schedule"] == schedule.model_dump(mode="json")
    assert EditProposalSnapshot.model_validate(dumped).frame_schedule == schedule


def test_public_proposal_response_redacts_private_planning_diagnostics() -> None:
    proposal = EditProposal(
        proposal_version=1,
        generation_attempt_id="attempt-1",
        status="draft",
        draft=_snapshot(),
        planning_diagnostics={"requested_frames": 90, "failure_reason": "private"},
    )
    response = EditProposalResponse.model_validate(proposal.model_dump(mode="json"))
    serialized = response.model_dump(mode="json")
    assert response.planning_diagnostics is None
    assert "planning_diagnostics" not in serialized
