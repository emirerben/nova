"""Boundary tests for semantic proposal planning and schedule refreshes."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from app.agents._runtime import ModelInvocation, TerminalError
from app.agents.edit_proposal import (
    EditProposalAgentInput,
    EditProposalAgentOutput,
    EditProposalMedia,
)
from app.agents.semantic_edit_proposal import SemanticEditProposalAgent
from app.schemas.edit_frame_schedule import EditFrameSchedule, FrameScheduledMoment
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MixedMediaTimingProfile,
    NarrationTrack,
    StoryBeat,
    canonical_media_digest,
)
from app.schemas.semantic_edit import SemanticEditPlan
from app.services.proposal_planning import (
    INFEASIBLE_INPUT_MESSAGE,
    SCHEDULE_REJECTED_MESSAGE,
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


@pytest.mark.parametrize("audio_kind", ["video", "image"])
def test_story_snapshot_carrying_montage_audio_checks_its_sources(audio_kind: str) -> None:
    """Planning copies ``montage_audio`` into every direction's plan: a story
    without fast cuts validates its audio sources instead of crashing."""

    base = _snapshot().model_dump()
    base["media"].append(
        MediaRef(
            lane="clip",
            media_id="clip-2",
            gcs_path="users/clip-2",
            generation="1",
            kind=audio_kind,
            duration_s=3 if audio_kind == "video" else None,
        ).model_dump()
    )
    audio = {"preserve_source_audio": True, "source_media_ids": ["clip-2"]}
    if audio_kind == "image":
        with pytest.raises(ValueError, match="montage audio beds require video sources"):
            EditProposalSnapshot.model_validate({**base, "montage_audio": audio})
        return
    snapshot = EditProposalSnapshot.model_validate({**base, "montage_audio": audio})
    assert snapshot.montage_audio is not None
    assert snapshot.montage_audio.source_media_ids == ["clip-2"]


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
    # The same confirmed inputs cannot pass, and the creator reads a written
    # sentence; the scheduler's own reason stays the private diagnostic.
    assert error.value.retryable is False
    assert error.value.message == INFEASIBLE_INPUT_MESSAGE
    assert "pinned timing" not in error.value.message


@pytest.mark.parametrize(
    ("code", "reason", "message"),
    [
        ("semantic_capacity", "scheduled source windows exceed footage capacity", None),
        ("conflicting_text_bindings", "One source has conflicting requested captions.", "same"),
    ],
)
def test_schedule_rejection_shows_written_copy_and_stays_retryable(
    monkeypatch: pytest.MonkeyPatch, code: str, reason: str, message: str | None
) -> None:
    from app.services.semantic_edit_scheduler import FeasibilityError, assess_semantic_feasibility

    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    monkeypatch.setattr(
        "app.agents.semantic_edit_proposal.SemanticEditProposalAgent.run",
        lambda *_args, **_kwargs: _semantic_plan(),
    )

    def reject(*_args, **_kwargs):
        raise FeasibilityError(code, reason, assess_semantic_feasibility(_input()))

    monkeypatch.setattr("app.services.semantic_edit_scheduler.schedule_semantic_edit", reject)
    with pytest.raises(SemanticPlanningError) as error:
        plan_edit_proposal(_input(), force_semantic=True)

    assert error.value.retryable is True
    assert error.value.reason == reason
    assert error.value.message == (reason if message == "same" else SCHEDULE_REJECTED_MESSAGE)


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


_SEMANTIC_GOLDEN = (
    Path(__file__).resolve().parents[1] / "fixtures/agent_evals/semantic_edit_proposal/golden"
)


def _narrated_snapshot(
    creator_input: EditProposalAgentInput,
    *,
    title: str,
    duration_s: float,
    story_beats: list[StoryBeat],
    frame_schedule: EditFrameSchedule,
    montage_text_bindings: list,
) -> EditProposalSnapshot:
    return EditProposalSnapshot(
        direction=creator_input.direction,
        goal=creator_input.goal,
        pace=creator_input.pace,
        duration_s=duration_s,
        title=title,
        opening_title=creator_input.opening_title,
        opening_title_duration_s=creator_input.opening_title_duration_s,
        closing_title=creator_input.closing_title,
        shot_labels=creator_input.shot_labels,
        media_scope=creator_input.media_scope,
        selected_media_ids=creator_input.selected_media_ids,
        video_reuse_policy=creator_input.video_reuse_policy,
        narration=NarrationTrack(
            gcs_path="fixtures/narration.wav",
            generation="1",
            duration_s=creator_input.narration_duration_s,
            words=creator_input.narration_words,
        ),
        clip_intents=creator_input.clip_intents,
        media=[
            MediaRef(
                media_id=item.media_id,
                lane=item.lane,
                kind=item.kind,
                duration_s=item.duration_s,
                gcs_path=f"fixtures/{item.media_id}",
                generation="1",
            )
            for item in creator_input.media
        ],
        story_beats=story_beats,
        frame_schedule=frame_schedule,
        montage_text_bindings=montage_text_bindings,
        mixed_media_timing=creator_input.mixed_media_timing,
    )


def _compiled_text_elements(snapshot: EditProposalSnapshot) -> list[dict]:
    """The same compiler v8 call as validate_proposal_compiles, keeping its text lanes."""
    from app.pipeline.guided_story import compile_execution_plan

    compiled = compile_execution_plan(
        {
            "proposal_version": 1,
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
        },
        track=None,
    )
    assert compiled["compiler_version"] == 8
    return compiled["text_elements"]


class _CassetteClient:
    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text
        self.calls = 0

    def invoke(self, **_kwargs: object) -> ModelInvocation:
        self.calls += 1
        return ModelInvocation(raw_text=self.raw_text)


def _plan_golden_replay(
    monkeypatch: pytest.MonkeyPatch, replay: dict
) -> tuple[EditProposalAgentInput, EditProposalAgentOutput, list[dict]]:
    """Run the real planning boundary on a recorded response, then compile its text."""
    creator_input = EditProposalAgentInput.model_validate(replay["input"])
    client = _CassetteClient(replay["raw_text"])
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: client)

    output = plan_edit_proposal(creator_input, force_semantic=True)

    assert client.calls == 1
    assert output.planning_diagnostics["outcome"] == "compiled"
    snapshot = _narrated_snapshot(
        creator_input,
        title=output.title,
        duration_s=output.duration_s,
        story_beats=output.scheduled_story_beats,
        frame_schedule=output.frame_schedule,
        montage_text_bindings=output.montage_text_bindings,
    )
    return creator_input, output, _compiled_text_elements(snapshot)


def test_narrated_reported_speech_replay_parses_schedules_and_compiles() -> None:
    """A story quotation must not become a mandatory on-screen caption."""
    from app.pipeline.guided_story import validate_proposal_compiles
    from app.services.semantic_edit_scheduler import schedule_semantic_edit

    fixture_path = _SEMANTIC_GOLDEN / "narrated_reported_speech.json"
    replay = json.loads(fixture_path.read_text())
    creator_input = EditProposalAgentInput.model_validate(replay["input"])
    assert len(creator_input.media) == 10
    assert len(creator_input.narration_words) == 91
    assert 'said: "The work can take its time."' in creator_input.creator_request

    plan = SemanticEditProposalAgent(None).parse(replay["raw_text"], creator_input)
    scheduled = schedule_semantic_edit(plan, creator_input)
    snapshot = _narrated_snapshot(
        creator_input,
        title=creator_input.opening_title or plan.title,
        duration_s=scheduled.duration_s,
        story_beats=scheduled.story_beats,
        frame_schedule=scheduled.schedule,
        montage_text_bindings=scheduled.montage_text_bindings,
    )

    validate_proposal_compiles(snapshot)
    assert len(plan.chapters) == 10
    assert len(snapshot.story_beats) == 10
    assert snapshot.frame_schedule.total_frames == 1458


@pytest.mark.parametrize(
    "fixture_name, image_count, chapter_count, repair",
    [
        # every voiceover sentence as both a thought and a chapter binding
        ("narrated_voiceover_line_bindings", 4, 8, "blanked_narrated_thought:7"),
        # shot directions as thoughts, voiceover sentences as bindings
        ("narrated_shot_direction_thoughts", 4, 10, "blanked_narrated_thought:9"),
        # a surplus empty payoff chapter on the invented alias m000
        ("narrated_invented_media_alias", 5, 7, "dropped_unknown_media_chapter:7"),
    ],
)
def test_narrated_voiceover_line_bindings_plan_title_and_captions_only(
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    image_count: int,
    chapter_count: int,
    repair: str,
) -> None:
    """Voiceover sentences returned as chapter copy never fail or reach the screen twice.

    Prod shape: a reused photo made two chapter-targeted sentence bindings
    conflict on one source (`conflicting_text_bindings`), and the thoughts
    would have drawn AI copy over the timed narration captions.
    """
    replay = json.loads((_SEMANTIC_GOLDEN / f"{fixture_name}.json").read_text())
    raw = json.loads(replay["raw_text"])
    assert len(raw["text_bindings"]) == 8
    assert Counter(media["kind"] for media in replay["input"]["media"]) == {
        "video": 5,
        "image": image_count,
    }

    creator_input, output, elements = _plan_golden_replay(monkeypatch, replay)

    assert {"dropped_non_montage_text_bindings:8", repair} <= set(output.repairs)
    assert output.frame_schedule.total_frames == 1459
    assert len(output.scheduled_story_beats) == chapter_count
    assert all(beat.thought == "" for beat in output.scheduled_story_beats)
    assert output.montage_text_bindings == []
    captions = [row for row in elements if row["id"].startswith("narration-caption-")]
    assert [row["id"] for row in elements if row not in captions] == ["guided-title"]
    assert " ".join(row["text"] for row in captions) == " ".join(
        word["text"] for word in creator_input.narration_words
    )


def test_narrated_quoted_display_caption_from_a_dropped_binding_still_renders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit creator copy that only came back as a binding moves to its chapter thought."""
    replay = json.loads((_SEMANTIC_GOLDEN / "narrated_voiceover_line_bindings.json").read_text())
    replay["input"]["creator_request"] += ' Put "Still standing" on screen over the last shot.'
    raw = json.loads(replay["raw_text"])
    raw["text_bindings"][-1] = {"text": "Still standing", "chapter_ids": ["chapter-8"]}
    replay["raw_text"] = json.dumps(raw)

    _creator_input, output, elements = _plan_golden_replay(monkeypatch, replay)

    assert "moved_creator_caption_binding_to_thought:7:7" in output.repairs
    assert [beat.thought for beat in output.scheduled_story_beats] == [""] * 7 + ["Still standing"]
    assert [
        (row["id"], row["text"])
        for row in elements
        if not row["id"].startswith("narration-caption-")
    ] == [
        ("guided-title", "One light on the coast"),
        ("guided-thought-chapter-8", "Still standing"),
    ]
