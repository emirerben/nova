"""KRI-129: the creator's prompt outranks every taste rule in EditProposalAgent.parse().

Product decision (non-negotiable): an AI-authored plan that follows the
creator's request must never be thrown away for a house taste rule. Before
this change, a single violated editorial rule (an oversized beat, a beat
count over 5, a distinct-source floor, an empty thought, an 18-word overrun,
...) raised SchemaError; after one retry that became TerminalError and
`edit_proposal_build.py` swapped in a request-blind deterministic plan (N
longest clips, no grouping) -- discarding a plan that was actually following
the creator's request faithfully.

This file covers, against both a synthetic 3/6/7-media shape (reusing the
helpers from test_edit_proposal_agent.py) and the real 16-clip production
shape (redacted job input, no identifiers) that triggered the incident:
  (a) the prod shape: a 6-clip "Post match pub" beat is split into
      consecutive <=4-media beats, preserving every clip and the total
      duration, and reporting the repair.
  (b) 12 story beats parse (old ceiling was 5).
  (c) a 2-beat / single-topic / 3-source story parses (old floors were beats
      >= 3, sources >= 7, topics >= 3).
  (d) empty thoughts parse (old rule: "thoughts cannot be empty").
  (e) a 25-word thought is truncated to 18 words; a first-person "personal
      experience" thought is blanked; the plan survives either way.
  (f) a declared duration far from the target is scaled to the target.
  (g) a fast montage cut list opening on `role: "build"` is relabelled, and
      a fast montage capacity-bound short of its target is accepted at its
      own achievable total.
  (h) the guards that ARE still real (not taste) still hold: an unknown
      media id, dropped required coverage, "once" video reuse, and reworded
      creator shot labels all still raise.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.agents._runtime import SchemaError
from app.agents.edit_proposal import EditProposalAgent, EditProposalAgentInput, EditProposalMedia
from app.schemas.edit_proposal import MontageAudioPlan
from tests.agents.test_edit_proposal_agent import _input as _seven_media_input
from tests.agents.test_edit_proposal_agent import _raw as _seven_media_raw
from tests.tasks.test_creator_shot_labels_contract import LABELS as _SHOT_LABELS
from tests.tasks.test_creator_shot_labels_contract import _agent_input as _labeled_agent_input
from tests.tasks.test_creator_shot_labels_contract import _labeled_output

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "kri129_sixteen_clip_pub_group.json"
)


def _load_pub_group_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _pub_group_input(**overrides: object) -> EditProposalAgentInput:
    fixture = _load_pub_group_fixture()
    values: dict[str, object] = {
        "direction": "guided_story",
        "goal": fixture["goal"],
        "creator_request": fixture["request"],
        "pace": fixture["pace"],
        "target_duration_s": fixture["duration_s"],
        "opening_title": fixture["opening_title"],
        "closing_title": fixture["closing_title"],
        "video_reuse_policy": fixture["video_reuse_policy"],
        "montage_audio": MontageAudioPlan(**fixture["montage_audio"]),
        "media": [
            EditProposalMedia(
                media_id=row["media_id"],
                lane=row["lane"],
                kind=row["kind"],
                duration_s=row.get("duration_s"),
                subject=row.get("subject", ""),
                description=row.get("description", ""),
                on_screen_text=row.get("on_screen_text", ""),
                best_moments=row.get("best_moments") or [],
            )
            for row in fixture["media"]
        ],
    }
    values.update(overrides)
    return EditProposalAgentInput(**values)


# One beat, "Post match pub", lists all 6 cafe/table clips at once -- the
# exact shape that triggered "story_beats.4.media_ids: List should have at
# most 4 items after validation, not 6" in production.
#
# clip-04.mp4 (1.267s) is under GUIDED_STORY_MIN_MOMENT_S (1.4s), so it is
# never offered to the model at all -- `shortlist_edit_proposal_media`
# filters sub-floor clips from the aliasable candidate set before the prompt
# is even built (pre-existing render-safety behavior, unrelated to KRI-129).
# It is the one clip this fixture's beats cannot reference.
PUB_GROUP_BEATS: list[tuple[str, list[str], float]] = [
    ("Park", ["clip-15.mp4", "clip-14.mp4"], 8.0),
    ("Sports", ["clip-16.mp4"], 6.0),
    ("Speech", ["clip-13.mp4", "clip-03.mp4", "clip-12.mp4"], 10.0),
    ("Street moments", ["clip-05.mp4", "clip-10.mp4", "clip-11.mp4"], 10.0),
    (
        "Post match pub",
        [
            "clip-01.mp4",
            "clip-02.mp4",
            "clip-06.mp4",
            "clip-07.mp4",
            "clip-08.mp4",
            "clip-09.mp4",
        ],
        26.0,
    ),
]


def _pub_group_raw_text() -> str:
    return json.dumps(
        {
            "title": "Emir Olympics London Edition",
            "duration_s": 60,
            "story_beats": [
                {
                    "topic": topic,
                    "thought": f"{topic} moments from the day.",
                    "media_ids": media_ids,
                    "layout": "fullscreen",
                    "duration_s": duration_s,
                }
                for topic, media_ids, duration_s in PUB_GROUP_BEATS
            ],
            "fast_cuts": None,
            "mixed_media_timing": None,
            "montage_text_bindings": [],
            "montage_audio": {
                "preserve_source_audio": True,
                "preview_source_beds": False,
                "source_media_ids": [],
            },
        }
    )


# ── (a) prod shape: oversized "Post match pub" beat splits, nothing lost ──────


def test_prod_shape_pub_group_beat_splits_without_losing_coverage() -> None:
    agent_input = _pub_group_input()

    output = EditProposalAgent(None).parse(_pub_group_raw_text(), agent_input)

    all_media_ids = {media.media_id for media in agent_input.media}
    used = {media_id for beat in output.story_beats for media_id in beat.media_ids}
    # clip-04.mp4 is structurally unreachable (see the comment on
    # PUB_GROUP_BEATS); every other clip must still appear somewhere.
    assert used == all_media_ids - {"clip-04.mp4"}

    pub_beats = [beat for beat in output.story_beats if beat.topic == "Post match pub"]
    assert len(pub_beats) == 2, "the 6-clip beat must split into consecutive same-topic parts"
    assert all(1 <= len(beat.media_ids) <= 4 for beat in pub_beats)
    assert sum(len(beat.media_ids) for beat in pub_beats) == 6
    assert [media_id for beat in pub_beats for media_id in beat.media_ids] == [
        "clip-01.mp4",
        "clip-02.mp4",
        "clip-06.mp4",
        "clip-07.mp4",
        "clip-08.mp4",
        "clip-09.mp4",
    ]
    assert math.isclose(sum(beat.duration_s for beat in pub_beats), 26.0, abs_tol=0.01)
    assert math.isclose(sum(beat.duration_s for beat in output.story_beats), 60.0, abs_tol=0.01)
    assert output.duration_s == 60
    assert any(repair.startswith("split_beat:") for repair in output.repairs)


# ── (b) 12 beats parse (old ceiling was 5) ─────────────────────────────────────


def test_twelve_beats_parse() -> None:
    agent_input = _seven_media_input(12)
    payload = {
        "title": "A dozen chapters",
        "duration_s": 24,
        "story_beats": [
            {
                "topic": f"Chapter {index + 1}",
                "thought": f"Detail {index + 1} worth noting.",
                "media_ids": [f"media-{index}"],
                "layout": "fullscreen",
                "duration_s": 2,
            }
            for index in range(12)
        ],
    }

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)

    assert len(output.story_beats) == 12


# ── (c) 2-beat / single-topic / 3-source story parses ──────────────────────────


def test_two_beat_single_topic_three_source_story_parses() -> None:
    agent_input = _seven_media_input(3)
    payload = {
        "title": "Two-beat story",
        "duration_s": 24,
        "story_beats": [
            {
                "topic": "Everything",
                "thought": "The first half of the day.",
                "media_ids": ["media-0", "media-1"],
                "layout": "fullscreen",
                "duration_s": 16,
            },
            {
                "topic": "Everything",
                "thought": "The second half of the day.",
                "media_ids": ["media-2"],
                "layout": "fullscreen",
                "duration_s": 8,
            },
        ],
    }

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)

    assert len(output.story_beats) == 2
    assert {beat.topic for beat in output.story_beats} == {"Everything"}
    assert {media_id for beat in output.story_beats for media_id in beat.media_ids} == {
        "media-0",
        "media-1",
        "media-2",
    }


# ── (d) empty thoughts parse ────────────────────────────────────────────────────


def test_empty_thoughts_parse() -> None:
    agent_input = _seven_media_input(3)
    payload = json.loads(_seven_media_raw(["media-0", "media-1", "media-2"]))
    for beat in payload["story_beats"]:
        beat["thought"] = ""

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)

    assert all(beat.thought == "" for beat in output.story_beats)


# ── (e) word-boundary truncation + personal-experience blanking ────────────────


def test_truncates_long_thought_and_blanks_personal_experience() -> None:
    agent_input = _seven_media_input(3)
    payload = json.loads(_seven_media_raw(["media-0", "media-1", "media-2"]))
    payload["story_beats"][0]["thought"] = " ".join(f"word{index}" for index in range(25))
    payload["story_beats"][1]["thought"] = "I loved every second of this delicious meal."

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)

    assert len(output.story_beats[0].thought.split()) == 18
    assert output.story_beats[0].thought == " ".join(f"word{index}" for index in range(18))
    assert output.story_beats[1].thought == ""
    assert "truncated_thought:0" in output.repairs
    assert "blanked_thought:1" in output.repairs


# ── (f) declared duration far from target is scaled ────────────────────────────


def test_declared_duration_far_off_target_is_scaled() -> None:
    agent_input = _seven_media_input(3)
    payload = json.loads(_seven_media_raw(["media-0", "media-1", "media-2"]))
    payload["duration_s"] = 44  # 20s off the 24s target

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)

    assert output.duration_s == 24
    assert math.isclose(sum(beat.duration_s for beat in output.story_beats), 24, abs_tol=0.01)
    assert "scaled_durations" in output.repairs


# ── (g) fast montage: role relabel + capacity-bound short total accepted ───────


def test_fast_montage_relabels_misplaced_first_role() -> None:
    media = [
        EditProposalMedia(media_id=f"media-{index}", lane="clip", kind="video", duration_s=4.0)
        for index in range(3)
    ]
    agent_input = EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=6,
        media=media,
    )
    payload = {
        "title": "Quick cuts",
        "duration_s": 6,
        "story_beats": [],
        "fast_cuts": [
            {
                "cut_id": f"cut-{index}",
                "media_id": f"media-{index}",
                "source_start_s": 0,
                "source_end_s": 2,
                "output_duration_s": 2,
                "role": "build",
            }
            for index in range(3)
        ],
    }

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)

    cuts = output.fast_cuts or []
    assert cuts[0].role == "hook"
    assert cuts[-1].role == "payoff"
    assert "relabelled_roles" in output.repairs


def test_fast_cut_window_past_the_end_of_its_clip_is_pulled_back_not_rejected() -> None:
    """Live replay of the prod montage lost a whole authored plan to "fast cut
    source window exceeds video". The window keeps its length and slides
    earlier, so the cut and the total are unchanged."""

    media = [
        EditProposalMedia(media_id=f"media-{index}", lane="clip", kind="video", duration_s=4.0)
        for index in range(3)
    ]
    agent_input = EditProposalAgentInput(
        direction="fast_montage", pace="fast", target_duration_s=6, media=media
    )
    payload = {
        "title": "Quick cuts",
        "duration_s": 6,
        "story_beats": [],
        "fast_cuts": [
            {
                "cut_id": f"cut-{index}",
                "media_id": f"media-{index}",
                # The middle cut asks for 2.6-4.6 s of a 4.0 s clip.
                "source_start_s": 2.6 if index == 1 else 0,
                "source_end_s": 4.6 if index == 1 else 2,
                "output_duration_s": 2,
                "role": "hook" if index == 0 else "payoff" if index == 2 else "build",
            }
            for index in range(3)
        ],
    }

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)

    cuts = output.fast_cuts or []
    assert (cuts[1].source_start_s, cuts[1].source_end_s) == (2.0, 4.0)
    assert math.isclose(sum(cut.output_duration_s for cut in cuts), 6.0, abs_tol=0.01)
    assert "clamped_cut_window:1" in output.repairs


def test_fast_montage_short_authored_total_is_accepted() -> None:
    # 8s short of the 20s target: four sources already used to their full
    # length under the default once-reuse policy, nothing left to extend.
    media = [
        EditProposalMedia(media_id=f"media-{index}", lane="clip", kind="video", duration_s=3.0)
        for index in range(4)
    ]
    agent_input = EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=20,
        media=media,
    )
    cuts = [
        {
            "cut_id": f"cut-{index}",
            "media_id": f"media-{index}",
            "source_start_s": 0,
            "source_end_s": 3.0,
            "output_duration_s": 3.0,
            "role": "hook" if index == 0 else "payoff" if index == 3 else "build",
        }
        for index in range(4)
    ]
    payload = {"title": "Short montage", "duration_s": 20, "story_beats": [], "fast_cuts": cuts}

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)

    assert output.duration_s == pytest.approx(12.0)
    assert sum(cut.output_duration_s for cut in output.fast_cuts or []) == pytest.approx(12.0)
    assert "accepted_short_fast_montage_total" in output.repairs


# ── (h) guards that ARE still real still hold ───────────────────────────────────


def test_guard_unknown_media_id_still_raises() -> None:
    agent_input = _seven_media_input(3)
    payload = json.loads(_seven_media_raw(["media-0", "media-1", "media-2"]))
    payload["story_beats"][0]["media_ids"] = ["unknown-media"]

    with pytest.raises(SchemaError, match="unknown media"):
        EditProposalAgent(None).parse(json.dumps(payload), agent_input)


def test_guard_media_scope_all_dropped_clip_still_raises() -> None:
    agent_input = _seven_media_input(3)
    agent_input.media_scope = "all"
    agent_input.selected_media_ids = [media.media_id for media in agent_input.media]
    payload = {
        "title": "Two clips only",
        "duration_s": 24,
        "story_beats": [
            {
                "topic": "First",
                "thought": "First detail.",
                "media_ids": ["media-0"],
                "layout": "fullscreen",
                "duration_s": 12,
            },
            {
                "topic": "Second",
                "thought": "Second detail.",
                "media_ids": ["media-1"],
                "layout": "fullscreen",
                "duration_s": 12,
            },
        ],
    }

    with pytest.raises(SchemaError, match="requested media coverage was dropped"):
        EditProposalAgent(None).parse(json.dumps(payload), agent_input)


def test_guard_video_reused_under_once_still_raises() -> None:
    agent_input = _seven_media_input(3)
    payload = {
        "title": "Reused video",
        "duration_s": 24,
        "story_beats": [
            {
                "topic": "First",
                "thought": "First detail.",
                "media_ids": ["media-0"],
                "layout": "fullscreen",
                "duration_s": 12,
            },
            {
                "topic": "Second",
                "thought": "Second detail.",
                "media_ids": ["media-0"],
                "layout": "fullscreen",
                "duration_s": 12,
            },
        ],
    }

    with pytest.raises(SchemaError, match="video source may appear only once"):
        EditProposalAgent(None).parse(json.dumps(payload), agent_input)


def test_guard_creator_shot_labels_reworded_still_raises() -> None:
    reworded = [*_SHOT_LABELS[:-1], "SAT · COMMON TOURIST ERRORS"]

    with pytest.raises(SchemaError, match="reworded or reordered"):
        EditProposalAgent(None).parse(json.dumps(_labeled_output(reworded)), _labeled_agent_input())


# ---------------------------------------------------------------------------
# Review findings: a repaired plan must still be renderable.
# ---------------------------------------------------------------------------


def _nine_two_second_clips() -> list[EditProposalMedia]:
    return [
        EditProposalMedia(media_id=f"c{index}", lane="clip", kind="video", duration_s=2.0)
        for index in range(9)
    ]


def _one_beat_naming_all_nine(duration_s: float) -> dict:
    return {
        "title": "Pub crawl",
        "duration_s": duration_s,
        "story_beats": [
            {
                "topic": "Pub",
                "thought": "Pub moments from the night.",
                "media_ids": [f"c{index}" for index in range(9)],
                "layout": "fullscreen",
                "duration_s": duration_s,
            }
        ],
    }


def test_story_naming_more_clips_than_the_duration_can_show_is_trimmed() -> None:
    """Nine clips need 9 x 1.4 s = 12.6 s; a 12 s story cannot show them all.
    The compiler only reports that in the render worker, after approval, so the
    planner drops the clips the creator did not require until the story fits."""

    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=12,
        media=_nine_two_second_clips(),
    )

    output = EditProposalAgent(None).parse(json.dumps(_one_beat_naming_all_nine(12)), agent_input)

    used = [media_id for beat in output.story_beats for media_id in beat.media_ids]
    assert len(used) == 8 == len(set(used))
    assert all(len(beat.media_ids) <= 4 for beat in output.story_beats)
    assert "dropped_media_for_duration:1" in output.repairs


def test_required_clips_that_cannot_fit_are_not_silently_dropped() -> None:
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=12,
        media_scope="all",
        selected_media_ids=[f"c{index}" for index in range(9)],
        media=_nine_two_second_clips(),
    )

    with pytest.raises(SchemaError, match="required clips cannot fit"):
        EditProposalAgent(None).parse(json.dumps(_one_beat_naming_all_nine(12)), agent_input)


def test_selected_short_clip_is_offered_required_and_kept() -> None:
    """A short video is never left out for being short. A selected 1.0 s clip
    used to be hidden from the model, so requiring it crashed prompt rendering
    with a KeyError on its missing alias. It is now offered, required, and
    plays for its own length."""

    media = [
        EditProposalMedia(media_id=f"clip-{name}", lane="clip", kind="video", duration_s=seconds)
        for name, seconds in (("A", 5.0), ("B", 4.0), ("C", 6.0), ("D", 4.5), ("E", 1.0))
    ]
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=15,
        media_scope="selected",
        selected_media_ids=[item.media_id for item in media],
        media=media,
    )

    prompt = EditProposalAgent(None).render_prompt(agent_input)
    assert "Reference every alias at least once" in prompt

    def payload(media_ids: list[str]) -> str:
        return json.dumps(
            {
                "title": "Five clips",
                "duration_s": 15,
                "story_beats": [
                    {
                        "topic": "Clips",
                        "thought": "",
                        "media_ids": media_ids,
                        "layout": "fullscreen",
                        "duration_s": 15,
                    }
                ],
            }
        )

    every_id = [item.media_id for item in media]
    output = EditProposalAgent(None).parse(payload(every_id), agent_input)
    assert {media_id for beat in output.story_beats for media_id in beat.media_ids} == set(every_id)
    assert "dropped_media_for_duration" not in " ".join(output.repairs)

    # Leaving the short clip out is a coverage violation like any other.
    with pytest.raises(SchemaError, match="requested media coverage was dropped"):
        EditProposalAgent(None).parse(payload(every_id[:4]), agent_input)


def test_fast_montage_cut_may_be_shorter_than_the_floor_only_for_a_whole_short_clip() -> None:
    """A 0.3 s clip plays for its own 0.3 s; a 0.2 s sliver of a long clip is
    still rejected, so the 0.4 s floor keeps its meaning for ordinary clips."""

    def parse(cut_media: str, cut_s: float) -> object:
        media = [
            EditProposalMedia(media_id="long-a", lane="clip", kind="video", duration_s=5.0),
            EditProposalMedia(media_id="long-b", lane="clip", kind="video", duration_s=5.0),
            EditProposalMedia(media_id="long-c", lane="clip", kind="video", duration_s=5.0),
            EditProposalMedia(media_id="tiny", lane="clip", kind="video", duration_s=0.3),
        ]
        agent_input = EditProposalAgentInput(
            direction="fast_montage", pace="fast", target_duration_s=4, media=media
        )
        middle_s = round(4 - 2 * 1.85, 3) if cut_media == "tiny" else cut_s
        cuts = [
            ("long-a", 1.85 if cut_media == "tiny" else (4 - cut_s) / 2, "hook"),
            (cut_media, middle_s, "build"),
            ("long-b", 1.85 if cut_media == "tiny" else (4 - cut_s) / 2, "payoff"),
        ]
        payload = {
            "title": "Quick cuts",
            "duration_s": 4,
            "story_beats": [],
            "fast_cuts": [
                {
                    "cut_id": f"cut-{index}",
                    "media_id": media_id,
                    "source_start_s": 0,
                    "source_end_s": round(length_s, 3),
                    "output_duration_s": round(length_s, 3),
                    "role": role,
                }
                for index, (media_id, length_s, role) in enumerate(cuts)
            ],
        }
        return EditProposalAgent(None).parse(json.dumps(payload), agent_input)

    output = parse("tiny", 0.3)
    tiny = next(cut for cut in output.fast_cuts or [] if cut.media_id == "tiny")
    assert tiny.output_duration_s == pytest.approx(0.3, abs=0.001)

    with pytest.raises(SchemaError, match="at least 0.4s"):
        parse("long-c", 0.2)


def test_trimming_for_length_never_drops_a_clip_a_creator_intent_names() -> None:
    """KRI-127 x KRI-129: 12 clips cannot all be shown in 16 s, and the creator
    asked to INCLUDE a 13th. The plan sheds optional clips, the requested clip
    is placed, and the story still fits what its length can show."""

    from tests.agents.test_edit_proposal_clip_intents import _include_intent  # noqa: PLC0415

    media = [
        EditProposalMedia(media_id=f"m{index}", lane="clip", kind="video", duration_s=3.0)
        for index in range(1, 13)
    ] + [EditProposalMedia(media_id="speech", lane="clip", kind="video", duration_s=3.0)]
    intent = _include_intent(["speech"])
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=16,
        media=media,
        clip_intents=[intent],
    )
    payload = {
        "title": "Full beats",
        "duration_s": 16,
        "story_beats": [
            {
                "topic": topic,
                "thought": "",
                "media_ids": [f"m{index}" for index in range(start, start + 4)],
                "layout": "fullscreen",
                "duration_s": duration_s,
            }
            for topic, start, duration_s in (("One", 1, 6), ("Two", 5, 5), ("Three", 9, 5))
        ],
    }

    output = EditProposalAgent(None).parse(json.dumps(payload), agent_input)

    used = [media_id for beat in output.story_beats for media_id in beat.media_ids]
    assert "speech" in used
    assert len(used) * 1.4 <= 16 + 1e-6
    assert any(repair.startswith("dropped_media_for_duration") for repair in output.repairs)
