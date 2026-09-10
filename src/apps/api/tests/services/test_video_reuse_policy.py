"""One appearance by default; explicit creator requests survive replanning."""

import json

import pytest
from pydantic import ValidationError

from app.agents._runtime import SchemaError, TerminalError
from app.agents.edit_proposal import EditProposalAgent, EditProposalAgentInput
from app.pipeline.guided_story import validate_proposal_timing
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    StoryBeat,
    resolve_video_reuse_policy,
)
from app.services import edit_direction_planner as planner


@pytest.mark.parametrize(
    ("words", "previous", "expected"),
    [
        ("A slow edit with pastel yellow Summer in Madrid text", None, "once"),
        ("Make it a longer video", None, "once"),
        ("Loop these videos to make a 20 second edit", None, "allow_repeat"),
        ("Please repeat the first clip at the end", None, "allow_repeat"),
        ("Make it loop", None, "allow_repeat"),
        ("Use the last shot again", None, "allow_repeat"),
        ("Alternate video clips using different portions", None, "distinct_windows"),
        ('Add the title "Repeat the video"', None, "once"),
        ("Loop the music", None, "once"),
        ("Repeat the title", None, "once"),
        ("Make the text yellow", "allow_repeat", "allow_repeat"),
        ("Stop looping the clips", "allow_repeat", "once"),
        ("Use every video only once", "allow_repeat", "once"),
        ("Don't repeat the footage", "allow_repeat", "once"),
    ],
)
def test_permission_is_grounded_in_creator_words(words, previous, expected):
    assert resolve_video_reuse_policy(words, previous) == expected


def media():
    return [
        MediaRef(
            lane="clip",
            media_id=f"clip-{index}",
            kind="video",
            gcs_path=f"users/test/clip-{index}.mp4",
            generation="1",
            duration_s=duration,
        )
        for index, duration in enumerate((3.733, 4.9, 4.068))
    ]


def snapshot(cuts, *, policy="once"):
    return EditProposalSnapshot(
        direction="fast_montage",
        title="Summer in Madrid",
        pace="relaxed",
        duration_s=sum(cut.output_duration_s for cut in cuts),
        media=media(),
        fast_cuts=cuts,
        video_reuse_policy=policy,
        story_beats=[
            StoryBeat(beat_id="opening", topic="Madrid", media_ids=["clip-0"], duration_s=3)
        ],
    )


def test_reported_three_video_case_uses_three_contiguous_cuts_and_compiles():
    cuts = planner.deterministic_fast_cuts(media(), 12)
    assert len(cuts) == len({cut.media_id for cut in cuts}) == 3
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(12)
    assert max(cut.output_duration_s for cut in cuts) > 3
    validate_proposal_timing(snapshot(cuts))


def test_longer_target_shortens_to_available_unique_footage():
    cuts = planner.deterministic_fast_cuts(media(), 60)
    assert sum(cut.output_duration_s for cut in cuts) == pytest.approx(12.7)
    assert len(cuts) == 3


def test_single_video_default_has_one_cut_explicit_loop_can_repeat_same_window():
    single = media()[:1]
    once = planner.deterministic_fast_cuts(single, 10)
    assert len(once) == 1
    assert once[0].output_duration_s == pytest.approx(112 / 30)
    repeated = planner.deterministic_fast_cuts(single, 6, video_reuse_policy="allow_repeat")
    assert len(repeated) > 1
    assert all(cut.source_start_s == 0 for cut in repeated)
    assert sum(cut.output_duration_s for cut in repeated) == pytest.approx(6)
    validate_proposal_timing(snapshot(repeated, policy="allow_repeat"))


def test_snapshot_rejects_different_windows_of_same_video_but_reads_legacy():
    cuts = [
        FastMontageCut(
            cut_id=f"cut-{i}",
            media_id=f"clip-{i % 2}",
            source_start_s=i // 2,
            source_end_s=i // 2 + 1,
            output_duration_s=1,
            role="hook" if i == 0 else "payoff" if i == 3 else "build",
        )
        for i in range(4)
    ]
    with pytest.raises(ValidationError, match="only once"):
        snapshot(cuts)
    legacy = snapshot(cuts, policy=None)
    assert "video_reuse_policy" not in legacy.model_dump(mode="json")
    validate_proposal_timing(legacy)


def test_agent_rejects_revisiting_sources_and_does_not_split_long_cuts():
    agent = EditProposalAgent(None)
    input_value = EditProposalAgentInput(
        direction="fast_montage",
        pace="relaxed",
        target_duration_s=12,
        media=[ref.model_dump() for ref in media()],
    )
    cuts = planner.deterministic_fast_cuts(media(), 12)
    raw = {
        "title": "Madrid",
        "duration_s": 12,
        "story_beats": [],
        "fast_cuts": [cut.model_dump() for cut in cuts],
    }
    output = agent.parse(json.dumps(raw), input_value)
    assert len(output.fast_cuts) == 3
    first = raw["fast_cuts"][0]
    half = first["output_duration_s"] / 2
    first["source_end_s"] = first["source_start_s"] + half
    first["output_duration_s"] = half
    raw["fast_cuts"].append(
        {
            **first,
            "cut_id": "return",
            "role": "payoff",
            "source_start_s": first["source_end_s"],
            "source_end_s": first["source_end_s"] + half,
        }
    )
    with pytest.raises(SchemaError, match="only once"):
        agent.parse(json.dumps(raw), input_value)


def test_generated_edit_can_opt_into_loops_then_remove_them(monkeypatch):
    class FailedAgent:
        def __init__(self, client):
            pass

        def run(self, value, **kwargs):
            raise TerminalError("exercise the fallback")

    monkeypatch.setattr(planner, "EditProposalAgent", FailedAgent)
    monkeypatch.setattr(planner, "default_client", lambda: None)
    original = snapshot(planner.deterministic_fast_cuts(media(), 12))

    def replan(source, words):
        return planner.plan_direction_snapshot(
            source,
            direction="fast_montage",
            goal="Madrid",
            pace="relaxed",
            duration_s=18,
            creator_request=words,
        )

    looped = replan(original, "Loop the videos to make it longer")
    assert looped.video_reuse_policy == "allow_repeat"
    assert looped.duration_s == 18
    assert len(looped.fast_cuts) > 3
    validate_proposal_timing(looped)
    kept = replan(looped, "Keep the yellow title")
    assert kept.video_reuse_policy == "allow_repeat"
    removed = replan(kept, "Stop looping the videos")
    assert removed.video_reuse_policy == "once"
    assert len(removed.fast_cuts) == 3
    assert removed.duration_s == pytest.approx(12.7)
    validate_proposal_timing(removed)


def test_native_degraded_match_drops_revisits_without_mutating_user_steps():
    from app.pipeline.agents.gemini_analyzer import AssemblyStep
    from app.tasks.generative_build import _single_use_video_steps

    steps = [
        AssemblyStep(
            slot={"position": i + 1, "target_duration_s": 1},
            clip_id=clip_id,
            moment={"start_s": 0, "end_s": 1},
        )
        for i, clip_id in enumerate(["a", "b", "a", "c", "b"])
    ]
    original_positions = [step.slot["position"] for step in steps]
    result = _single_use_video_steps(steps)
    assert [step.clip_id for step in result] == ["a", "b", "c"]
    assert [step.slot["position"] for step in result] == [1, 2, 3]
    assert [step.slot["position"] for step in steps] == original_positions


def test_guided_chapters_cannot_revisit_a_video_by_default():
    once = snapshot(planner.deterministic_fast_cuts(media(), 12))
    payload = once.model_dump()
    payload.update(
        direction="guided_story",
        fast_cuts=None,
        story_beats=[
            {
                "beat_id": f"beat-{i}",
                "topic": f"Scene {i}",
                "media_ids": [f"clip-{i % 2}"],
                "duration_s": 3,
            }
            for i in range(3)
        ],
    )
    with pytest.raises(ValidationError, match="only once"):
        EditProposalSnapshot.model_validate(payload)
    payload.pop("video_reuse_policy")
    assert EditProposalSnapshot.model_validate(payload).video_reuse_policy is None
