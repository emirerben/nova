"""Boundary tests for semantic proposal planning and schedule refreshes."""

from __future__ import annotations

import pytest

from app.agents._runtime import TerminalError
from app.agents.edit_proposal import EditProposalAgentInput, EditProposalMedia
from app.schemas.edit_frame_schedule import EditFrameSchedule, FrameScheduledMoment
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MixedMediaTimingProfile,
    StoryBeat,
)
from app.schemas.semantic_edit import SemanticEditPlan
from app.services.proposal_planning import (
    SemanticPlanningError,
    plan_edit_proposal,
    refresh_snapshot_schedule,
)


def _input(*, target: float = 3, direction: str = "guided_story") -> EditProposalAgentInput:
    return EditProposalAgentInput(
        direction=direction,
        pace="fast" if direction == "fast_montage" else "balanced",
        target_duration_s=target,
        media=[
            EditProposalMedia(
                media_id="clip-1",
                lane="clip",
                kind="video",
                duration_s=3,
                best_moments=[{"start_s": 0, "end_s": 3}],
            )
        ],
    )


def _semantic_plan() -> SemanticEditPlan:
    return SemanticEditPlan(
        title="A clip",
        chapters=[
            {
                "chapter_id": "chapter-1",
                "topic": "Start",
                "role": "hook",
                "sources": [{"media_id": "clip-1", "candidate_index": 0}],
            }
        ],
    )


def _snapshot(
    *, schedule: EditFrameSchedule | None = None, title: str = "A clip"
) -> EditProposalSnapshot:
    return EditProposalSnapshot(
        frame_schedule=schedule,
        direction="guided_story",
        pace="balanced",
        duration_s=3,
        title=title,
        media=[
            MediaRef(
                lane="clip",
                media_id="clip-1",
                gcs_path="users/clip.mp4",
                generation="1",
                kind="video",
                duration_s=3,
            )
        ],
        story_beats=[
            StoryBeat(beat_id="beat-1", topic="Start", media_ids=["clip-1"], duration_s=3)
        ],
    )


def _schedule(*, source_start: int = 0) -> EditFrameSchedule:
    return EditFrameSchedule(
        total_frames=90,
        transition_frames=0,
        direction="guided_story",
        moments=[
            FrameScheduledMoment(
                moment_id="beat-1",
                beat_id="beat-1",
                media_id="clip-1",
                source_start_frame=source_start,
                source_end_frame=source_start + 90,
                output_start_frame=0,
                output_end_frame=90,
                role="hook",
            )
        ],
    )


def test_preflight_infeasible_never_invokes_semantic_model(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def should_not_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("semantic model must not run after infeasible preflight")

    monkeypatch.setattr(
        "app.agents.semantic_edit_proposal.SemanticEditProposalAgent.run", should_not_run
    )
    impossible = _input(target=10).model_copy(update={"narration_duration_s": 10})
    with pytest.raises(SemanticPlanningError, match="pinned timing") as error:
        plan_edit_proposal(impossible, force_semantic=True)

    assert error.value.code == "semantic_edit_infeasible"
    assert error.value.diagnostics["outcome"] == "infeasible"
    assert called is False


def test_semantic_success_returns_server_schedule_and_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    monkeypatch.setattr(
        "app.agents.semantic_edit_proposal.SemanticEditProposalAgent.run",
        lambda *_args, **_kwargs: _semantic_plan(),
    )

    output = plan_edit_proposal(_input(), force_semantic=True)

    assert output.frame_schedule is not None
    assert output.frame_schedule.total_frames == 90
    assert output.planning_diagnostics["outcome"] == "compiled"
    assert output.planning_diagnostics["schedule"] == output.frame_schedule.model_dump(mode="json")
    assert output.semantic_plan["chapters"][0]["chapter_id"] == "chapter-1"


def test_semantic_terminal_error_is_not_silently_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    monkeypatch.setattr(
        "app.agents.semantic_edit_proposal.SemanticEditProposalAgent.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TerminalError("model refused")),
    )

    with pytest.raises(SemanticPlanningError) as error:
        plan_edit_proposal(_input(), force_semantic=True)

    assert error.value.code == "semantic_edit_planning_failed"
    assert error.value.diagnostics["outcome"] == "semantic_rejected"


def test_text_only_refresh_discards_forged_schedule_and_preserves_trusted_windows() -> None:
    previous = _snapshot(schedule=_schedule())
    forged = _snapshot(schedule=_schedule(source_start=1), title="Creator title")

    refreshed = refresh_snapshot_schedule(forged, previous=previous)

    assert refreshed.frame_schedule == previous.frame_schedule
    assert refreshed.title == "Creator title"


def test_guided_timing_edit_regenerates_schedule() -> None:
    previous = _snapshot(schedule=_schedule())
    revised = _snapshot(schedule=None).model_copy(
        update={
            "story_beats": [
                StoryBeat(
                    beat_id="beat-1",
                    topic="New",
                    media_ids=["clip-1"],
                    layout="supporting_card",
                    duration_s=3,
                )
            ]
        }
    )

    refreshed = refresh_snapshot_schedule(revised, previous=previous)

    assert refreshed.frame_schedule is not None
    assert refreshed.frame_schedule.total_frames == 90
    assert refreshed.frame_schedule != previous.frame_schedule


def test_fast_cuts_quantize_to_frames_and_reject_total_mismatch() -> None:
    media = [
        MediaRef(
            lane="clip",
            media_id="clip-1",
            gcs_path="users/1.mp4",
            generation="1",
            kind="video",
            duration_s=3,
        ),
        MediaRef(
            lane="clip",
            media_id="clip-2",
            gcs_path="users/2.mp4",
            generation="2",
            kind="video",
            duration_s=3,
        ),
    ]
    cuts = [
        FastMontageCut(
            cut_id="cut-1",
            media_id="clip-1",
            source_start_s=0.001,
            source_end_s=1.501,
            output_duration_s=1.5,
            role="hook",
        ),
        FastMontageCut(
            cut_id="cut-2",
            media_id="clip-2",
            source_start_s=1.499,
            source_end_s=2.999,
            output_duration_s=1.5,
            role="payoff",
        ),
    ]
    snapshot = EditProposalSnapshot(
        direction="fast_montage",
        pace="fast",
        duration_s=3,
        title="Fast",
        media=media,
        story_beats=[
            StoryBeat(beat_id="cut-1", topic="One", media_ids=["clip-1"], duration_s=1.5),
            StoryBeat(beat_id="cut-2", topic="Two", media_ids=["clip-2"], duration_s=1.5),
        ],
        fast_cuts=cuts,
        mixed_media_timing=MixedMediaTimingProfile(
            image_hold="very_fast",
            video_hold="longer",
            boundary_style="cut",
        ),
    )
    refreshed = refresh_snapshot_schedule(snapshot)
    assert [(cut.source_start_s, cut.source_end_s) for cut in refreshed.fast_cuts or []] == [
        (0.0, 1.5),
        (1.5, 3.0),
    ]

    with pytest.raises(Exception, match="durations must match"):
        refresh_snapshot_schedule(snapshot.model_copy(update={"duration_s": 4}))


@pytest.mark.parametrize("repeated", [False, True])
def test_layout_revision_preserves_split_labels_and_repeated_sources(repeated: bool) -> None:
    from app.services.semantic_edit_scheduler import schedule_semantic_edit

    media = [
        EditProposalMedia(
            media_id=f"clip-{index}",
            lane="clip",
            kind="video",
            duration_s=3 if repeated else 5,
        )
        for index in range(1 if repeated else 6)
    ]
    input = _input(target=8 if repeated else 12).model_copy(
        update={
            "media": media,
            "media_scope": "all",
            "shot_labels": ["Creator label"],
            "video_reuse_policy": "allow_repeat" if repeated else "once",
        }
    )
    plan = SemanticEditPlan(
        title="Creator edit",
        chapters=[
            {
                "chapter_id": "creator-group",
                "topic": "Group",
                "thought": "Creator label",
                "role": "hook",
                "sources": [{"media_id": item.media_id} for item in media],
            }
        ],
    )
    scheduled = schedule_semantic_edit(plan, input)
    previous = EditProposalSnapshot(
        title=plan.title,
        direction=input.direction,
        pace=input.pace,
        duration_s=scheduled.duration_s,
        shot_labels=input.shot_labels,
        media_scope="all",
        video_reuse_policy=input.video_reuse_policy,
        media=[
            MediaRef(
                media_id=item.media_id,
                lane=item.lane,
                kind=item.kind,
                duration_s=item.duration_s,
                gcs_path=f"users/{item.media_id}.mp4",
                generation="1",
            )
            for item in media
        ],
        story_beats=scheduled.story_beats,
        frame_schedule=scheduled.schedule,
    )
    if repeated:
        assert previous.story_beats[0].media_ids.count("clip-0") > 1
    else:
        assert len(previous.story_beats) == 2
    revised = previous.model_copy(deep=True)
    revised.frame_schedule = None
    for beat in revised.story_beats:
        beat.layout = "supporting_card"

    refreshed = refresh_snapshot_schedule(revised, previous=previous)

    assert refreshed.frame_schedule.total_frames == scheduled.schedule.total_frames
    assert all(moment.layout == "supporting_card" for moment in refreshed.frame_schedule.moments)
    assert all(beat.thought == "Creator label" for beat in refreshed.story_beats)
    assert all(beat.thought_source == "user" for beat in refreshed.story_beats)
    assert refreshed.frame_schedule.direction == "guided_story"
