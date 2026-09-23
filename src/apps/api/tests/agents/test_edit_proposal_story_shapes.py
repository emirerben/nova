"""KRI-118 lane L3: chat-picked story shapes (day_vlog / single_hero) applied
to the guided-story specialist, plus the fast_montage taste-rule removal.

Per the KRI-129 "creator's prompt wins" rule, every constraint here is
classified as either a genuine render limit (repaired deterministically and
signaled to the caller, never silent) or taste (removed outright). See
`app.services.story_shapes` for the deterministic repair layer and
`app.tasks.edit_proposal_build` for the feasibility/title fixes covered by
their own test modules.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agents.edit_proposal import (
    DraftStoryBeat,
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalAgentOutput,
    EditProposalMedia,
)
from app.pipeline.guided_story import validate_proposal_compiles
from app.schemas.edit_proposal import EditProposalSnapshot, MediaRef, StoryBeat
from app.services import story_shapes

# ---------------------------------------------------------------------------
# 1. fast_montage taste-rule removal: a valid montage may use just one kind.
# ---------------------------------------------------------------------------


def _fast_cuts_payload(media_ids: list[str]) -> dict:
    return {
        "title": "Montage",
        "duration_s": len(media_ids) * 0.75,
        "story_beats": [],
        "fast_cuts": [
            {
                "cut_id": f"cut-{index}",
                "media_id": media_id,
                "source_start_s": 0,
                "source_end_s": 0.75,
                "output_duration_s": 0.75,
                "role": (
                    "hook" if index == 0 else "payoff" if index == len(media_ids) - 1 else "build"
                ),
            }
            for index, media_id in enumerate(media_ids)
        ],
    }


def test_fast_montage_photos_only_is_accepted() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    media_ids = [f"photo-{i}" for i in range(1, 5)]
    agent_input = EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=3,
        media=[
            EditProposalMedia(media_id=media_id, lane="asset", kind="image")
            for media_id in media_ids
        ],
    )
    output = agent.parse(json.dumps(_fast_cuts_payload(media_ids)), agent_input)
    assert {cut.media_id for cut in output.fast_cuts or []} == set(media_ids)


def test_fast_montage_videos_only_is_accepted() -> None:
    agent = EditProposalAgent(None)  # type: ignore[arg-type]
    media_ids = [f"clip-{i}" for i in range(1, 5)]
    agent_input = EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=3,
        media=[
            EditProposalMedia(media_id=media_id, lane="clip", kind="video", duration_s=2.0)
            for media_id in media_ids
        ],
    )
    output = agent.parse(json.dumps(_fast_cuts_payload(media_ids)), agent_input)
    assert {cut.media_id for cut in output.fast_cuts or []} == set(media_ids)


# ---------------------------------------------------------------------------
# 2. story_shapes pure functions (unit-level: duck-typed stand-ins).
# ---------------------------------------------------------------------------


def _beat(media_ids: list[str], *, duration_s: float = 4.0, transition_duration_s=None):
    return SimpleNamespace(
        media_ids=list(media_ids),
        duration_s=duration_s,
        transition_duration_s=transition_duration_s,
    )


def _output(beats: list) -> SimpleNamespace:
    return SimpleNamespace(story_beats=beats)


def _media_ref(media_id: str, *, kind: str = "video", duration_s: float = 5.0):
    return SimpleNamespace(media_id=media_id, kind=kind, duration_s=duration_s)


def _stub_input(media: list, *, clip_intents=None, hero_media_id=None):
    return SimpleNamespace(media=media, clip_intents=clip_intents, hero_media_id=hero_media_id)


def test_repair_day_vlog_reorders_into_attachment_order() -> None:
    media = [_media_ref("a"), _media_ref("b"), _media_ref("c")]
    beats = [_beat(["c"]), _beat(["a"]), _beat(["b"])]
    output = _output(beats)

    repairs = story_shapes.repair_day_vlog(output, _stub_input(media))

    assert [beat.media_ids for beat in output.story_beats] == [["a"], ["b"], ["c"]]
    assert "day_vlog_reordered_by_attachment" in repairs


def test_repair_day_vlog_never_overrides_an_explicit_pinned_order() -> None:
    media = [_media_ref("a"), _media_ref("b"), _media_ref("c")]
    beats = [_beat(["c"]), _beat(["a"]), _beat(["b"])]
    output = _output(beats)
    pinned_order_intent = SimpleNamespace(status="resolved", op="order")

    repairs = story_shapes.repair_day_vlog(
        output, _stub_input(media, clip_intents=[pinned_order_intent])
    )

    assert [beat.media_ids for beat in output.story_beats] == [["c"], ["a"], ["b"]]
    assert "day_vlog_reordered_by_attachment" not in repairs


def test_repair_day_vlog_clamps_transitions_over_the_cap() -> None:
    media = [_media_ref("a"), _media_ref("b")]
    beats = [_beat(["a"], transition_duration_s=0.5), _beat(["b"], transition_duration_s=0.1)]
    output = _output(beats)

    repairs = story_shapes.repair_day_vlog(output, _stub_input(media))

    assert output.story_beats[0].transition_duration_s == story_shapes.DAY_VLOG_TRANSITION_CAP_S
    assert output.story_beats[1].transition_duration_s == 0.1
    assert "day_vlog_transition_clamped" in repairs


def test_repair_day_vlog_refuses_fewer_than_two_moments() -> None:
    media = [_media_ref("a")]
    output = _output([_beat(["a"])])

    with pytest.raises(story_shapes.StoryShapeInfeasibleError):
        story_shapes.repair_day_vlog(output, _stub_input(media))


def test_repair_single_hero_reorders_hero_to_open() -> None:
    media = [_media_ref("a"), _media_ref("hero"), _media_ref("c")]
    beats = [_beat(["a"]), _beat(["hero"]), _beat(["c"])]
    output = _output(beats)

    repairs = story_shapes.repair_single_hero(output, _stub_input(media, hero_media_id="hero"))

    assert output.story_beats[0].media_ids == ["hero"]
    assert "single_hero_reordered_to_open" in repairs


def test_repair_single_hero_grows_share_past_the_largest_rival() -> None:
    media = [_media_ref("hero"), _media_ref("rival")]
    beats = [_beat(["hero"], duration_s=2.0), _beat(["rival"], duration_s=10.0)]
    output = _output(beats)

    repairs = story_shapes.repair_single_hero(output, _stub_input(media, hero_media_id="hero"))

    totals = story_shapes._source_screen_time_s(output.story_beats)
    assert totals["hero"] > totals["rival"]
    assert "single_hero_share_grown" in repairs
    # Screen time is redistributed, not invented.
    assert sum(beat.duration_s for beat in output.story_beats) == pytest.approx(12.0)


def test_repair_single_hero_includes_a_missing_hero() -> None:
    media = [_media_ref("a"), _media_ref("hero"), _media_ref("b")]
    beats = [_beat(["a"], duration_s=4.0), _beat(["b"], duration_s=4.0)]
    output = _output(beats)

    repairs = story_shapes.repair_single_hero(output, _stub_input(media, hero_media_id="hero"))

    assert any("hero" in beat.media_ids for beat in output.story_beats)
    assert "single_hero_included" in repairs


def test_repair_single_hero_single_clip_fallback_does_not_apply_the_shape() -> None:
    media = [_media_ref("only")]
    beats = [_beat(["only"], duration_s=6.0)]
    output = _output(beats)

    repairs = story_shapes.repair_single_hero(output, _stub_input(media, hero_media_id="only"))

    assert repairs == ["single_hero_single_clip_fallback"]
    assert output.story_beats[0].media_ids == ["only"]
    assert output.story_beats[0].duration_s == 6.0


def test_repair_single_hero_is_a_noop_without_hero_media_id() -> None:
    media = [_media_ref("a"), _media_ref("b")]
    output = _output([_beat(["a"]), _beat(["b"])])

    assert story_shapes.repair_single_hero(output, _stub_input(media, hero_media_id=None)) == []


def test_humanize_repairs_maps_known_codes_and_drops_unknown() -> None:
    codes = [
        "scaled_durations",
        "blanked_unrequested_thought:2",
        "day_vlog_transition_clamped",
        "single_hero_single_clip_fallback",
        "totally_unknown_code:7",
    ]

    assert story_shapes.humanize_repairs(codes) == [
        "Rescaled chapter lengths to match your target duration",
        "Removed an on-screen caption you didn't ask for",
        "Shortened a transition to fit the day-vlog pace",
        "Only one usable clip, so this rendered as a simple edit",
    ]


def test_humanize_repairs_collapses_duplicate_messages() -> None:
    codes = ["truncated_thought:0", "truncated_thought:1"]

    assert story_shapes.humanize_repairs(codes) == ["Shortened an on-screen caption"]


# ---------------------------------------------------------------------------
# 3. Repaired story-shape plans still compile (KRI-129 lesson: a repair that
#    looks fine at the schema level can still fail to render).
# ---------------------------------------------------------------------------


def _snapshot_from_repaired_output(
    output: EditProposalAgentOutput, real_input: EditProposalAgentInput
) -> EditProposalSnapshot:
    # Uses each source's OWN declared duration (not a value derived from the
    # repaired beat durations) so the compile dry-run is a genuine capacity
    # check, not a tautology.
    return EditProposalSnapshot(
        direction="guided_story",
        pace="balanced",
        duration_s=output.duration_s,
        title=output.title,
        media=[
            MediaRef(
                lane="clip",
                media_id=media.media_id,
                gcs_path=f"users/u/{media.media_id}.mp4",
                generation="1",
                kind="video",
                duration_s=media.duration_s,
            )
            for media in real_input.media
        ],
        story_beats=[
            StoryBeat(
                beat_id=f"beat-{index}",
                topic=beat.topic,
                thought=beat.thought,
                media_ids=beat.media_ids,
                duration_s=beat.duration_s,
            )
            for index, beat in enumerate(output.story_beats)
        ],
    )


def test_day_vlog_repaired_plan_compiles() -> None:
    beats = [
        DraftStoryBeat(topic="C", media_ids=["c"], duration_s=4.0),
        DraftStoryBeat(topic="A", media_ids=["a"], duration_s=4.0),
        DraftStoryBeat(topic="B", media_ids=["b"], duration_s=4.0),
    ]
    output = EditProposalAgentOutput(title="A day", duration_s=12.0, story_beats=beats)
    real_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=12,
        story_shape="day_vlog",
        media=[
            # Slightly longer than each beat's 4.0s weight so there is room
            # for the compiler's own transition overlap accounting.
            EditProposalMedia(media_id="a", lane="clip", kind="video", duration_s=4.5),
            EditProposalMedia(media_id="b", lane="clip", kind="video", duration_s=4.5),
            EditProposalMedia(media_id="c", lane="clip", kind="video", duration_s=4.5),
        ],
    )

    story_shapes.repair_day_vlog(output, real_input)
    assert [beat.media_ids for beat in output.story_beats] == [["a"], ["b"], ["c"]]

    validate_proposal_compiles(_snapshot_from_repaired_output(output, real_input))


def test_single_hero_repaired_plan_compiles() -> None:
    beats = [
        DraftStoryBeat(topic="Support", media_ids=["rival"], duration_s=10.0),
        DraftStoryBeat(topic="Hero", media_ids=["hero"], duration_s=2.0),
    ]
    output = EditProposalAgentOutput(title="The hero", duration_s=12.0, story_beats=beats)
    real_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=12,
        story_shape="single_hero",
        hero_media_id="hero",
        media=[
            # Hero's own filmed length comfortably covers its grown share
            # (deficit ~8.2s on top of its starting 2.0s beat).
            EditProposalMedia(media_id="hero", lane="clip", kind="video", duration_s=15.0),
            EditProposalMedia(media_id="rival", lane="clip", kind="video", duration_s=10.0),
        ],
    )

    story_shapes.repair_single_hero(output, real_input)
    assert output.story_beats[0].media_ids == ["hero"]

    validate_proposal_compiles(_snapshot_from_repaired_output(output, real_input))
