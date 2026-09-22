import json
from pathlib import Path

import pytest

from app.agents.edit_proposal import EditProposalAgentInput, EditProposalMedia
from app.schemas.edit_proposal import MixedMediaTimingProfile, MontageCadenceConstraint
from app.schemas.semantic_edit import SemanticEditPlan
from app.services.semantic_edit_scheduler import (
    FeasibilityError,
    assess_semantic_feasibility,
    schedule_semantic_edit,
)


def _input(*, direction="fast_montage", target=15, **updates):
    pace = updates.pop("pace", "balanced")
    media = updates.pop(
        "media",
        [
            EditProposalMedia(
                media_id=f"clip-{index}",
                lane="clip",
                kind="video",
                duration_s=10,
                best_moments=[{"start_s": 9, "end_s": 10}],
            )
            for index in range(3)
        ],
    )
    return EditProposalAgentInput(
        direction=direction, pace=pace, target_duration_s=target, media=media, **updates
    )


def _plan(ids=("clip-0", "clip-1", "clip-2"), *, candidate=True):
    return SemanticEditPlan(
        title="story",
        chapters=[
            {
                "chapter_id": f"chapter-{index}",
                "topic": "story",
                "role": "hook" if index == 0 else "payoff",
                "sources": [
                    {"media_id": media_id, **({"candidate_index": 0} if candidate else {})}
                ],
            }
            for index, media_id in enumerate(ids)
        ],
    )


def test_late_candidate_uses_full_source_capacity_and_is_deterministic():
    first = schedule_semantic_edit(_plan(), _input())
    second = schedule_semantic_edit(_plan(), _input())

    assert first.schedule == second.schedule
    assert first.schedule.total_frames == 450
    assert [(m.source_start_frame, m.source_end_frame) for m in first.schedule.moments] == [
        (150, 300),
        (150, 300),
        (150, 300),
    ]


@pytest.mark.parametrize("direction", ["guided_story", "fast_montage", "text_explainer"])
def test_all_directions_end_at_the_exact_frame(direction):
    result = schedule_semantic_edit(_plan(), _input(direction=direction))

    assert result.schedule.moments[-1].output_end_frame == 450
    assert result.schedule.direction == direction
    assert (result.fast_cuts is not None) is (direction == "fast_montage")


def test_fixed_narration_uses_ceil_frame_duration():
    result = schedule_semantic_edit(_plan(), _input(target=4, narration_duration_s=4.001))

    assert result.schedule.total_frames == 121


def test_reuse_policies_enforce_once_and_disjoint_windows():
    plan = _plan(("clip-0", "clip-0"))
    with pytest.raises(FeasibilityError, match="reuse_once"):
        schedule_semantic_edit(plan, _input(target=6))

    result = schedule_semantic_edit(plan, _input(target=6, video_reuse_policy="distinct_windows"))
    assert [(m.source_start_frame, m.source_end_frame) for m in result.schedule.moments] == [
        (0, 90),
        (90, 180),
    ]


def test_explicit_round_robin_cadence_is_preserved():
    result = schedule_semantic_edit(
        _plan(("clip-0", "clip-1", "clip-0", "clip-1")),
        _input(
            target=4,
            video_reuse_policy="allow_repeat",
            montage_cadence=MontageCadenceConstraint(
                source_media_ids=["clip-0", "clip-1"], cut_duration_s=1, reuse_policy="allow_repeat"
            ),
        ),
    )
    assert [cut.output_duration_s for cut in result.fast_cuts or []] == [1, 1, 1, 1]


@pytest.mark.parametrize(
    ("direction", "pace", "expected_overlap"),
    [
        ("guided_story", "relaxed", 6),
        ("guided_story", "balanced", 4),
        ("guided_story", "fast", 0),
        ("text_explainer", "balanced", 4),
    ],
)
def test_shared_transition_policy_is_frame_exact(direction, pace, expected_overlap):
    result = schedule_semantic_edit(_plan(), _input(direction=direction, pace=pace))

    assert result.schedule.transition_frames == expected_overlap
    assert result.schedule.moments[-1].output_end_frame == result.schedule.total_frames


def test_allow_repeat_can_fill_a_target_larger_than_unique_source_capacity():
    media = [
        EditProposalMedia(media_id="clip-0", lane="clip", kind="video", duration_s=10),
    ]
    plan = _plan(("clip-0",), candidate=False)

    result = schedule_semantic_edit(
        plan, _input(target=15, media=media, video_reuse_policy="allow_repeat")
    )

    assert result.schedule.total_frames == 450
    assert len(result.schedule.moments) == 2


def test_fixed_photo_hold_rejects_sub_proposal_capacity():
    media = [EditProposalMedia(media_id="photo", lane="asset", kind="image")]
    report = assess_semantic_feasibility(
        _input(target=3, media=media, mixed_media_timing=MixedMediaTimingProfile(image_hold_s=0.3))
    )
    assert report.status == "infeasible"
    assert report.max_frames == 9


def test_unknown_candidate_is_a_visible_error():
    plan = SemanticEditPlan(
        chapters=[
            {
                "chapter_id": "chapter",
                "topic": "story",
                "role": "hook",
                "sources": [{"media_id": "clip-0", "candidate_index": 99}],
            }
        ]
    )
    with pytest.raises(FeasibilityError, match="requested candidate"):
        schedule_semantic_edit(plan, _input(target=3))


def test_kri129_capacity_fixture_has_a_frame_exact_fast_schedule():
    fixture = (
        Path(__file__).parents[1]
        / "fixtures"
        / "agent_evals"
        / "edit_proposal"
        / "golden"
        / "kri129_fast_montage_target_equals_capacity.json"
    )
    payload = json.loads(fixture.read_text())
    agent_input = EditProposalAgentInput.model_validate(payload["input"])
    plan = _plan(tuple(media.media_id for media in agent_input.media), candidate=False)

    result = schedule_semantic_edit(plan, agent_input)

    assert result.schedule.total_frames == 1800
    assert result.schedule.moments[-1].output_end_frame == 1800


def test_preflight_rejects_required_guided_coverage_below_three_seconds():
    media = [
        EditProposalMedia(media_id=f"clip-{index}", lane="clip", kind="video", duration_s=1)
        for index in range(3)
    ]
    report = assess_semantic_feasibility(
        _input(direction="guided_story", pace="relaxed", target=3, media=media, media_scope="all")
    )
    assert report.status == "infeasible"
    assert report.max_frames == 78


def test_preflight_rejects_fixed_mixed_required_minimum_that_exceeds_target():
    media = [
        EditProposalMedia(media_id=f"photo-{index}", lane="asset", kind="image")
        for index in range(7)
    ]
    report = assess_semantic_feasibility(
        _input(
            target=3,
            media=media,
            media_scope="all",
            mixed_media_timing=MixedMediaTimingProfile(image_hold_s=0.8),
        )
    )
    assert report.status == "infeasible"
    assert report.min_frames > 90


def test_preflight_requires_duration_for_any_selectable_video():
    report = assess_semantic_feasibility(
        _input(
            target=3,
            media=[EditProposalMedia(media_id="unknown", lane="clip", kind="video")],
        )
    )
    assert report.status == "infeasible"
    assert report.reason == "video duration is unknown"


def test_normal_fast_cut_rejects_sub_proposal_capacity():
    media = [EditProposalMedia(media_id="short", lane="clip", kind="video", duration_s=0.2)]
    with pytest.raises(FeasibilityError, match="minimum three-second"):
        schedule_semantic_edit(_plan(("short",), candidate=False), _input(target=3, media=media))


def test_optional_capacity_includes_all_selected_transition_overlaps():
    media = [
        EditProposalMedia(media_id=f"clip-{i}", lane="clip", kind="video", duration_s=10)
        for i in range(2)
    ]
    input = _input(direction="guided_story", target=20, media=media)
    report = assess_semantic_feasibility(input)
    result = schedule_semantic_edit(_plan(("clip-0", "clip-1"), candidate=False), input)
    assert report.status == "clamped"
    assert report.effective_frames == result.schedule.total_frames == 596


def test_expanded_bindings_deduplicate_and_conflicts_fail_closed():
    plan = _plan()
    plan.text_bindings = [
        {"text": "Exact", "chapter_ids": ["chapter-0"]},
        {"text": "Exact", "media_ids": ["clip-0"]},
    ]
    plan = SemanticEditPlan.model_validate(plan.model_dump())
    result = schedule_semantic_edit(plan, _input())
    assert [(binding.media_id, binding.text) for binding in result.montage_text_bindings] == [
        ("clip-0", "Exact")
    ]
    plan.text_bindings[1].text = "Conflicting"
    with pytest.raises(FeasibilityError, match="conflicting requested captions"):
        schedule_semantic_edit(plan, _input())


def test_creator_labels_retain_user_provenance():
    plan = _plan()
    labels = ["One", "Two", "Three"]
    for chapter, label in zip(plan.chapters, labels, strict=True):
        chapter.thought = label
    result = schedule_semantic_edit(plan, _input(direction="guided_story", shot_labels=labels))
    assert [beat.thought_source for beat in result.story_beats] == ["user"] * 3


def test_subframe_short_source_can_be_saved_without_overrun():
    from app.schemas.edit_proposal import EditProposalSnapshot, MediaRef

    media = [
        EditProposalMedia(media_id="short", lane="clip", kind="video", duration_s=0.21),
        EditProposalMedia(media_id="long", lane="clip", kind="video", duration_s=4),
    ]
    input = _input(target=3, media=media, media_scope="all")
    result = schedule_semantic_edit(_plan(("short", "long"), candidate=False), input)
    snapshot = EditProposalSnapshot(
        direction=input.direction,
        pace=input.pace,
        duration_s=result.duration_s,
        title="Day",
        media=[
            MediaRef(**row.model_dump(), gcs_path=f"users/{row.media_id}", generation="1")
            for row in media
        ],
        story_beats=result.story_beats,
        fast_cuts=result.fast_cuts,
        frame_schedule=result.schedule,
        video_reuse_policy=input.video_reuse_policy,
    )
    assert snapshot.fast_cuts[0].source_end_s == 0.2
    assert snapshot.frame_schedule.total_frames == 90
