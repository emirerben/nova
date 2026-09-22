"""KRI-127 Lane P: the planner honors chat-resolved clip intents as binding
structural constraints (group/order/include). Labels are never authored here
-- they are rendered deterministically by another lane.

`test_no_intents_prompt_is_byte_identical_to_pre_kri127` pins the sha256 of
the rendered prompt for a fixed input with no clip_intents, computed against
the pre-Lane-P code (git HEAD before this change). Any edit that makes the
no-intents path diverge -- even by one added blank line -- fails this test.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.edit_proposal import EditProposalAgent, EditProposalAgentInput, EditProposalMedia
from app.schemas.clip_intents import ClipAssignment, ResolvedClipIntent

# The no-intents prompt, for exactly the input `_canonical_input()` builds
# below with clip_intents left unset. It pins that an absent/empty intent list
# adds NOTHING to the prompt. Re-baselined for KRI-129 (prompt 1.15.0), which
# changed the base prompt itself (creator-request-outranks-guidelines rule,
# advisory beat/source guidance); recompute it whenever the base prompt changes.
_PRE_KRI127_PROMPT_SHA256 = "f55e42c8241d33df14b65f21b5207003878998dde0b93c04716ff84acea1215d"


def _canonical_input(**overrides: object) -> EditProposalAgentInput:
    media = [
        EditProposalMedia(
            media_id="clip_park",
            lane="clip",
            kind="video",
            duration_s=5.0,
            subject="a quiet park path",
            setting="a city park",
            activity="walking outdoors",
        ),
        EditProposalMedia(
            media_id="clip_soccer",
            lane="clip",
            kind="video",
            duration_s=5.0,
            subject="a soccer match",
            setting="a sports field",
            activity="playing soccer",
        ),
        EditProposalMedia(
            media_id="clip_volleyball",
            lane="clip",
            kind="video",
            duration_s=5.0,
            subject="a volleyball rally",
            setting="a sports field",
            activity="playing volleyball",
        ),
        EditProposalMedia(
            media_id="clip_speech",
            lane="clip",
            kind="video",
            duration_s=5.0,
            subject="a person talking to the camera",
            setting="a park bench",
            activity="talking to the camera",
            speaks_to_camera=True,
            transcript="hey everyone, quick update",
        ),
        EditProposalMedia(
            media_id="clip_pub",
            lane="clip",
            kind="video",
            duration_s=5.0,
            subject="a group raising glasses",
            setting="a neighborhood pub",
            activity="celebrating with drinks",
        ),
    ]
    fields: dict[object, object] = {
        "idea": "A day out with friends",
        "theme": "Park then pub",
        "direction": "guided_story",
        "goal": "Group the footage by where it happened",
        "creator_request": "Group the pub videos together and keep the park clips separate.",
        "pace": "balanced",
        "target_duration_s": 25,
        "media": media,
    }
    fields.update(overrides)
    return EditProposalAgentInput(**fields)  # type: ignore[arg-type]


def _five_beat_raw(*, pub_thought: str = "The group heads to the pub after.") -> str:
    """A compliant plan: park first, soccer/volleyball, speech, pub last."""

    beats = [
        {
            "topic": "Park",
            "thought": "A quiet stretch of the park with no one around.",
            "media_ids": ["clip_park"],
            "layout": "fullscreen",
            "duration_s": 5,
        },
        {
            "topic": "Soccer",
            "thought": "A soccer match plays out on the field.",
            "media_ids": ["clip_soccer"],
            "layout": "fullscreen",
            "duration_s": 5,
        },
        {
            "topic": "Volleyball",
            "thought": "A volleyball rally continues nearby.",
            "media_ids": ["clip_volleyball"],
            "layout": "fullscreen",
            "duration_s": 5,
        },
        {
            "topic": "Talking",
            "thought": "A moment talking straight to the camera.",
            "media_ids": ["clip_speech"],
            "layout": "fullscreen",
            "duration_s": 5,
        },
        {
            "topic": "Pub",
            "thought": pub_thought,
            "media_ids": ["clip_pub"],
            "layout": "fullscreen",
            "duration_s": 5,
        },
    ]
    return json.dumps({"title": "Park then pub", "duration_s": 25, "story_beats": beats})


def _order_intent(
    position: str, media_ids: list[str], intent_id: str = "i-order"
) -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id=intent_id,
        op="order",
        attribute="park clips where nobody is playing",
        position=position,
        assignments=[ClipAssignment(media_id=mid, confidence=0.8) for mid in media_ids],
    )


def _group_intent(
    media_ids: list[str], *, creator_text: str | None = None, intent_id: str = "i-group"
) -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id=intent_id,
        op="group",
        attribute="pub videos",
        creator_text=creator_text,
        assignments=[ClipAssignment(media_id=mid, confidence=0.8) for mid in media_ids],
    )


def _include_intent(media_ids: list[str], intent_id: str = "i-include") -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id=intent_id,
        op="include",
        attribute="where I talk to the camera",
        assignments=[ClipAssignment(media_id=mid, confidence=0.8) for mid in media_ids],
    )


def _label_intent(
    assignments: list[tuple[str, str]], intent_id: str = "i-label"
) -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id=intent_id,
        op="label",
        attribute="sport being played",
        assignments=[
            ClipAssignment(media_id=mid, value=value, confidence=0.85, grounding="vision_verified")
            for mid, value in assignments
        ],
    )


# ── Byte-identical prompt when unused ───────────────────────────────────────


def test_no_intents_prompt_is_byte_identical_to_pre_kri127() -> None:
    agent_input = _canonical_input()
    assert agent_input.clip_intents is None
    prompt = EditProposalAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == _PRE_KRI127_PROMPT_SHA256


def test_empty_clip_intents_list_is_also_a_no_op() -> None:
    with_none = EditProposalAgent(None).render_prompt(_canonical_input())  # type: ignore[arg-type]
    with_empty = EditProposalAgent(None).render_prompt(  # type: ignore[arg-type]
        _canonical_input(clip_intents=[])
    )
    assert with_none == with_empty


# ── Prompt renders binding constraints in alias terms only ─────────────────


def test_clip_intents_note_uses_aliases_only_never_a_real_media_id() -> None:
    agent_input = _canonical_input(
        clip_intents=[
            _order_intent("first", ["clip_park"]),
            _group_intent(["clip_pub"], creator_text="post match pub"),
            _include_intent(["clip_speech"]),
            _label_intent([("clip_soccer", "Soccer"), ("clip_volleyball", "Volleyball")]),
        ]
    )
    prompt = EditProposalAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]

    assert "CLIP INTENT CONSTRAINTS" in prompt
    assert "ORDER (first)" in prompt
    assert 'GROUP ("post match pub")' in prompt
    assert "post match pub" in prompt  # creator's own words, verbatim
    assert "INCLUDE" in prompt
    assert 'LABELS ("sport being played")' in prompt
    assert "Soccer" in prompt and "Volleyball" in prompt
    for media in agent_input.media:
        assert media.media_id not in prompt


def test_clip_intents_note_omits_shot_labels_precedence() -> None:
    """shot_labels wins outright; the clip-intents note stays empty."""

    agent_input = _canonical_input(
        shot_labels=["Park", "Soccer", "Volleyball", "Talking", "Pub"],
        clip_intents=[_order_intent("first", ["clip_park"])],
    )
    prompt = EditProposalAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]
    assert "CLIP INTENT CONSTRAINTS" not in prompt


def test_needs_creator_intent_is_not_rendered() -> None:
    unresolved = ResolvedClipIntent(
        intent_id="i-unresolved",
        op="group",
        attribute="pub videos",
        status="needs_creator",
        question="Which clips are the pub ones?",
    )
    agent_input = _canonical_input(clip_intents=[unresolved])
    prompt = EditProposalAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]
    assert "CLIP INTENT CONSTRAINTS" not in prompt


def test_media_not_in_input_is_dropped_from_the_prompt() -> None:
    agent_input = _canonical_input(
        clip_intents=[_include_intent(["clip_speech", "not-a-real-media-id"])]
    )
    prompt = EditProposalAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]
    assert "not-a-real-media-id" not in prompt
    assert "INCLUDE" in prompt  # clip_speech alone still renders


# ── parse(): compliant output honors every intent ───────────────────────────


def test_compliant_output_parses_and_honors_every_intent() -> None:
    agent_input = _canonical_input(
        clip_intents=[
            _order_intent("first", ["clip_park"]),
            _order_intent("last", ["clip_pub"], intent_id="i-order-last"),
            _group_intent(["clip_pub"], creator_text="post match pub"),
            _include_intent(["clip_speech"]),
            _label_intent([("clip_soccer", "Soccer"), ("clip_volleyball", "Volleyball")]),
        ]
    )
    output = EditProposalAgent(None).parse(  # type: ignore[arg-type]
        _five_beat_raw(pub_thought="post match pub"), agent_input
    )
    assert output.story_beats[0].media_ids == ["clip_park"]
    assert output.story_beats[-1].media_ids == ["clip_pub"]
    used = {mid for beat in output.story_beats for mid in beat.media_ids}
    assert "clip_speech" in used


def test_needs_creator_intent_is_ignored_by_validation() -> None:
    """An unresolved intent's (violated) implied constraint never blocks parse()."""

    unresolved_order_last = ResolvedClipIntent(
        intent_id="i-unresolved",
        op="order",
        attribute="the pub clip",
        position="last",
        status="needs_creator",
        question="Is this the last clip?",
        assignments=[ClipAssignment(media_id="clip_park", confidence=0.5)],
    )
    agent_input = _canonical_input(clip_intents=[unresolved_order_last])
    # clip_park is actually first in this raw output -- a resolved
    # order(last) intent on it would fail; the needs_creator status must
    # mean it is never checked.
    output = EditProposalAgent(None).parse(_five_beat_raw(), agent_input)  # type: ignore[arg-type]
    assert output.story_beats[0].media_ids == ["clip_park"]


def test_media_not_in_input_does_not_block_a_compliant_plan() -> None:
    agent_input = _canonical_input(
        clip_intents=[_include_intent(["clip_speech", "not-a-real-media-id"])]
    )
    output = EditProposalAgent(None).parse(_five_beat_raw(), agent_input)  # type: ignore[arg-type]
    assert output is not None


def test_shot_labels_precedence_skips_clip_intent_validation() -> None:
    """Both contracts present: shot_labels wins, clip intents are not checked."""

    labels = ["Park", "Soccer", "Volleyball", "Talking", "Pub"]
    agent_input = _canonical_input(
        shot_labels=labels,
        # This order(last) constraint on clip_park would fail against the
        # labeled beat order below (clip_park stays first) if it were ever
        # checked -- proving the skip, not just an accidental pass.
        clip_intents=[_order_intent("last", ["clip_park"])],
    )
    raw = json.dumps(
        {
            "title": "Park then pub",
            "duration_s": 25,
            "story_beats": [
                {
                    "topic": f"Beat {index}",
                    "thought": label,
                    "media_ids": [media_id],
                    "layout": "fullscreen",
                    "duration_s": 5,
                }
                for index, (label, media_id) in enumerate(
                    zip(
                        labels,
                        ["clip_park", "clip_soccer", "clip_volleyball", "clip_speech", "clip_pub"],
                        strict=True,
                    )
                )
            ],
        }
    )
    output = EditProposalAgent(None).parse(raw, agent_input)  # type: ignore[arg-type]
    assert output.story_beats[0].thought == "Park"
    assert output.story_beats[0].media_ids == ["clip_park"]


# ── parse(): violations raise a specific, alias-terms clarification ────────


def test_order_first_violation_that_cannot_be_repaired_raises() -> None:
    # clip_park (order first) and clip_soccer swap positions AND clip_soccer
    # is also asked to be last, so no reorder can satisfy both -- forcing a
    # genuine, unrepairable violation.
    agent_input = _canonical_input(
        clip_intents=[
            _order_intent("first", ["clip_soccer"]),
            _order_intent("last", ["clip_soccer"], intent_id="i-order-last"),
        ]
    )
    with pytest.raises(SchemaError, match="ORDER"):
        EditProposalAgent(None).parse(_five_beat_raw(), agent_input)  # type: ignore[arg-type]


def test_group_split_across_a_foreign_beat_raises() -> None:
    beats = json.loads(_five_beat_raw())
    # Split the (single-media) pub group by inserting an unrelated beat that
    # reuses clip_pub's neighbor topic in between two pub-labeled beats.
    beats["story_beats"] = [
        {
            "topic": "Pub",
            "thought": "The night starts at the pub.",
            "media_ids": ["clip_pub"],
            "layout": "fullscreen",
            "duration_s": 4,
        },
        {
            "topic": "Park",
            "thought": "A quiet stretch of the park with no one around.",
            "media_ids": ["clip_park"],
            "layout": "fullscreen",
            "duration_s": 4,
        },
        {
            "topic": "Soccer",
            "thought": "A soccer match plays out on the field.",
            "media_ids": ["clip_soccer"],
            "layout": "fullscreen",
            "duration_s": 4,
        },
        {
            "topic": "Volleyball",
            "thought": "A volleyball rally continues nearby.",
            "media_ids": ["clip_volleyball"],
            "layout": "fullscreen",
            "duration_s": 4,
        },
        {
            "topic": "Pub",
            "thought": "post match pub",
            "media_ids": ["clip_speech"],
            "layout": "fullscreen",
            "duration_s": 5,
        },
    ]
    agent_input = _canonical_input(
        clip_intents=[_group_intent(["clip_pub", "clip_speech"], creator_text="post match pub")]
    )
    with pytest.raises(SchemaError, match="GROUP"):
        EditProposalAgent(None).parse(json.dumps(beats), agent_input)  # type: ignore[arg-type]


def test_group_larger_than_four_across_consecutive_beats_is_accepted() -> None:
    media = [
        EditProposalMedia(media_id=f"pub-{i}", lane="clip", kind="video", duration_s=3.0)
        for i in range(5)
    ] + [EditProposalMedia(media_id="park-1", lane="clip", kind="video", duration_s=5.0)]
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=20,
        media=media,
        clip_intents=[_group_intent([f"pub-{i}" for i in range(5)], creator_text="post match pub")],
    )
    raw = json.dumps(
        {
            "title": "Pub night",
            "duration_s": 20,
            "story_beats": [
                {
                    "topic": "Pub",
                    "thought": "post match pub",
                    "media_ids": ["pub-0", "pub-1", "pub-2", "pub-3"],
                    "layout": "fullscreen",
                    "duration_s": 12,
                },
                {
                    "topic": "Pub continues",
                    "thought": "post match pub continues",
                    "media_ids": ["pub-4"],
                    "layout": "fullscreen",
                    "duration_s": 3,
                },
                {
                    "topic": "Park",
                    "thought": "A quiet moment in the park.",
                    "media_ids": ["park-1"],
                    "layout": "fullscreen",
                    "duration_s": 5,
                },
            ],
        }
    )
    output = EditProposalAgent(None).parse(raw, agent_input)  # type: ignore[arg-type]
    assert len(output.story_beats) == 3


def test_include_missing_entirely_raises_when_no_beat_has_room() -> None:
    # 12 regular sources (3 beats x the 4-media cap, satisfying the >=3
    # distinct-topics floor) plus one "speech" clip the INCLUDE intent
    # requires but the plan never uses and no beat has room left for.
    media = [
        EditProposalMedia(media_id=f"m{i}", lane="clip", kind="video", duration_s=3.0)
        for i in range(1, 13)
    ] + [EditProposalMedia(media_id="speech", lane="clip", kind="video", duration_s=3.0)]
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        # 12 clips need 12 x 1.4 s = 16.8 s. Below that the planner sheds an optional
        # clip to stay renderable (KRI-129), which would open a slot for the INCLUDE.
        target_duration_s=20,
        media=media,
        clip_intents=[_include_intent(["speech"])],
    )
    # Every beat is already at the 4-media cap, so the repair has nowhere to
    # place the missing "speech" media -- this must raise, not silently drop.
    raw = json.dumps(
        {
            "title": "Full beats",
            "duration_s": 16,
            "story_beats": [
                {
                    "topic": "One",
                    "thought": "A first shared moment together.",
                    "media_ids": ["m1", "m2", "m3", "m4"],
                    "layout": "fullscreen",
                    "duration_s": 6,
                },
                {
                    "topic": "Two",
                    "thought": "A second shared moment together.",
                    "media_ids": ["m5", "m6", "m7", "m8"],
                    "layout": "fullscreen",
                    "duration_s": 5,
                },
                {
                    "topic": "Three",
                    "thought": "A third shared moment together.",
                    "media_ids": ["m9", "m10", "m11", "m12"],
                    "layout": "fullscreen",
                    "duration_s": 5,
                },
            ],
        }
    )
    with pytest.raises(SchemaError, match="INCLUDE"):
        EditProposalAgent(None).parse(raw, agent_input)  # type: ignore[arg-type]


# ── parse(): deterministic repair ───────────────────────────────────────────


def test_order_first_violation_is_repaired_by_reordering_beats() -> None:
    beats = json.loads(_five_beat_raw())
    # Swap park (order-first) with soccer so park is no longer first.
    beats["story_beats"][0], beats["story_beats"][1] = (
        beats["story_beats"][1],
        beats["story_beats"][0],
    )
    agent_input = _canonical_input(clip_intents=[_order_intent("first", ["clip_park"])])
    output = EditProposalAgent(None).parse(json.dumps(beats), agent_input)  # type: ignore[arg-type]
    assert output.story_beats[0].media_ids == ["clip_park"]


def test_missing_include_is_repaired_into_a_beat_with_room() -> None:
    media = [
        EditProposalMedia(media_id="m1", lane="clip", kind="video", duration_s=5.0),
        EditProposalMedia(media_id="m2", lane="clip", kind="video", duration_s=5.0),
        EditProposalMedia(media_id="m3", lane="clip", kind="video", duration_s=5.0),
        EditProposalMedia(media_id="speech", lane="clip", kind="video", duration_s=5.0),
    ]
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        pace="balanced",
        target_duration_s=15,
        media=media,
        clip_intents=[_include_intent(["speech"])],
    )
    raw = json.dumps(
        {
            "title": "Three beats",
            "duration_s": 15,
            "story_beats": [
                {
                    "topic": "One",
                    "thought": "A first shared moment.",
                    "media_ids": ["m1"],
                    "layout": "fullscreen",
                    "duration_s": 5,
                },
                {
                    "topic": "Two",
                    "thought": "A second shared moment.",
                    "media_ids": ["m2"],
                    "layout": "fullscreen",
                    "duration_s": 5,
                },
                {
                    "topic": "Three",
                    "thought": "A third shared moment.",
                    "media_ids": ["m3"],
                    "layout": "fullscreen",
                    "duration_s": 5,
                },
            ],
        }
    )
    output = EditProposalAgent(None).parse(raw, agent_input)  # type: ignore[arg-type]
    used = {mid for beat in output.story_beats for mid in beat.media_ids}
    assert "speech" in used
