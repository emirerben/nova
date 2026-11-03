"""Parent KRI-1151: "Place them in between my speech" (with or without "when I talk about them")
for clips that have speech or sound of their own is an INSERT at the end of the sentence about
the clip, not an overlay. "Show it while I talk / over my speech / keep my voice" stays the
overlay.

Prod thread b61b7673 (2026-11-03): the planner sent the request to the full-screen overlay by
design (#2058: "in between my speech together with when I talk about them is the clip lane"),
so the talk kept running and the creator was muted under each clip. The creator meant the
opposite: "Instead of pausing my speech and giving the other one, it kept my speech going on".

Failure modes, written before the code (each is pinned below):

 T1 a row with neither `at_phrase` nor `at_s` is rejected or dropped, so the ask cannot be
    recorded without inventing a phrase; or a row with BOTH still parses;
 T2 the repair drops a position-less row, or keeps one the phone cannot render;
 T3 the prompt still routes "in between my speech" to the overlay, or routes "over my speech /
    while I keep talking / keep my voice" to the insert, in either lane combination;
 T4 a clip with no speech or sound of its own (the drone shot) is sent to the insert;
 T5 the brief extractor records the clips as `display_mode` + `clip_sound` for the same ask;
 T6 the corrective re-run note tells the model an "in between my speech" ask is an overlay;
 T7 a draft says "Done" for a clip the render may leave out, or a finished video says "Done"
    when only some clips played, or a reason has no Turkish twin;
 T8 a closed insert gate changes a single byte of any prompt (pinned in the lanes test).
"""

from __future__ import annotations

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.reaction_beats import InsertClip
from app.kria import ask_refusals
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import InsertClipFact, plan_facts_from_phone_variant
from app.services import creator_capabilities as capabilities
from tests.kria.test_insert_and_cutaway_lanes import (
    INSERT_MARK,
    SECTION_MARK,
    _extractor_prompt,
    _lanes,
    _prompt,
)
from tests.kria.test_insert_clip_planner import (
    FOOTAGE_B,
    FOOTAGE_C,
    _facts_for_draft,
    _julio_manifest,
    _open,
    _repair,
    _strategy,
    _verdict,
)
from tests.kria.test_planner_knows_talking_cutaways import _flat

BETWEEN = "Place them in between my speech. Show the talks in full screen and use their audio."


def _topic_req() -> BriefRequirement:
    return BriefRequirement(
        id="r1",
        kind="style",
        scope="global",
        description=BETWEEN,
        facts={"insert_clip": "the talks"},
    )


# --- T1, T2: the row ------------------------------------------------------------------------------


def test_t1_a_row_with_no_position_is_valid_and_means_the_sentence_about_the_clip():
    row = InsertClip(media_id=FOOTAGE_C)
    assert (row.at_phrase, row.at_s, row.keep_audio) == (None, None, True)
    assert row.model_dump(mode="json") == {"media_id": FOOTAGE_C, "keep_audio": True}
    with pytest.raises(ValueError):
        InsertClip(media_id=FOOTAGE_C, at_phrase="x", at_s=3)


def test_t1_a_strategy_keeps_position_less_rows_and_still_drops_a_row_with_two_positions():
    strategy = CreativeStrategy(
        edit_format="subtitled",
        insert_clips=[
            {"media_id": FOOTAGE_C},
            {"media_id": FOOTAGE_B, "keep_audio": False},
            {"media_id": "x", "at_phrase": "a", "at_s": 3},
        ],
    )
    assert [(r.media_id, r.keep_audio) for r in strategy.insert_clips] == [
        (FOOTAGE_C, True),
        (FOOTAGE_B, False),
    ]


def test_t2_the_repair_keeps_a_position_less_row_and_names_why_it_drops_one(monkeypatch):
    _open(monkeypatch)
    manifest = _julio_manifest()
    rows = [{"media_id": FOOTAGE_C}, {"media_id": FOOTAGE_B}]
    repaired, notices = _repair(manifest, _strategy(manifest, insert_clips=rows))
    assert notices == []
    assert [r.media_id for r in repaired.insert_clips] == [FOOTAGE_C, FOOTAGE_B]
    assert all(r.at_phrase is None and r.at_s is None for r in repaired.insert_clips)
    _open(monkeypatch, insert=False)
    dropped, notices = _repair(manifest, _strategy(manifest, insert_clips=rows))
    assert dropped.insert_clips is None
    assert notices == [capabilities.INSERT_CLIP_NOTICES["insert_clips_off"]] * 2


# --- T3, T4: the prompt ------------------------------------------------------------------------------


def _insert_section(monkeypatch, *, cutaways: bool) -> str:
    manifest = _lanes(monkeypatch, cutaways=cutaways, inserts=True)
    flat = _flat(_prompt(manifest))
    return flat[flat.index(INSERT_MARK) :]


@pytest.mark.parametrize("cutaways", [True, False])
def test_t3_in_between_my_speech_for_a_clip_with_its_own_sound_is_an_insert_at_the_sentence(
    monkeypatch, cutaways
):
    insert = _insert_section(monkeypatch, cutaways=cutaways)

    for wording in (
        '"place them in between my speech"',
        "with or without",
        '"when I talk about them"',
    ):
        assert wording in insert, wording
    assert "speech or sound of their own" in insert
    assert "no `at_phrase` and no `at_s`" in insert
    assert "end of the sentence the creator says about that clip" in insert
    assert "chosen by its meaning" in insert
    assert "left out and reported" in insert
    assert "whatever it says about sound" not in insert
    assert "is the full-screen clip lane above, whatever" not in insert


def test_t3_over_my_speech_while_i_keep_talking_and_keep_my_voice_stay_the_overlay(monkeypatch):
    insert = _insert_section(monkeypatch, cutaways=True)

    for wording in ("over my speech", "while I keep talking", "keep my voice"):
        assert wording in insert, wording
    assert "the full-screen clip lane above" in insert
    # The cutaway section no longer lists "in between my speech" as one of its own phrasings.
    flat = _flat(_prompt(_lanes(monkeypatch, cutaways=True, inserts=True)))
    cutaway = flat[flat.index(SECTION_MARK) : flat.index(INSERT_MARK)]
    assert '"place them in between my speech"' not in cutaway
    assert "over my speech" in cutaway
    assert "or to put a clip with speech" in cutaway
    assert "in between my speech" in cutaway


def test_t3_both_lanes_open_still_allow_exactly_one_clarifying_question_for_unanchored_wording(
    monkeypatch,
):
    insert = _insert_section(monkeypatch, cutaways=True)

    assert "use `ask_user` ONCE" in insert
    assert "at most one" in insert or "ONCE" in insert
    assert "in the middle of my talk" in insert


def test_t4_a_clip_with_no_speech_or_sound_stays_an_overlay_even_in_between_my_speech(
    monkeypatch,
):
    insert = _insert_section(monkeypatch, cutaways=True)

    assert "no speech or sound of its own" in insert
    assert "stays the clip shown over the talk" in insert


def test_t3_with_the_cutaways_closed_the_insert_is_still_the_only_in_between_form(monkeypatch):
    insert = _insert_section(monkeypatch, cutaways=False)

    assert "full-screen clip lane" not in insert
    assert "leave `insert_clips` empty" in insert


# --- T5: the brief --------------------------------------------------------------------------------------------


def test_t5_the_brief_records_in_between_my_speech_as_one_insert_clip_fact_with_no_position():
    flat = _flat(_extractor_prompt(insert_clips_open=True))
    start = flat.index("IN THE MIDDLE of the talk")
    rule = flat[start : flat.index("On a montage,", start)]

    assert "in between my speech" in rule
    assert '"insert_clip"' in rule
    assert "record no position" in rule
    assert "do not also record a `clip_sound` requirement" in rule
    assert "over my speech" in rule


# --- T6: the corrective note --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "capability", [ask_refusals.CAPABILITY_MENTION, ask_refusals.CAPABILITY_SOUND]
)
def test_t6_the_note_points_in_between_my_speech_at_insert_clips_when_that_lane_is_open(capability):
    note = _flat(ask_refusals.correction_note(capability, inserts_open=True))

    assert "in between my speech" in note
    assert "no phrase" in note
    assert "over my speech" in note


# --- T7: the receipts --------------------------------------------------------------------------------------------------


def test_t7_a_draft_does_not_promise_a_clip_the_render_may_leave_out(monkeypatch):
    _open(monkeypatch)
    manifest = _julio_manifest()
    strategy = _strategy(manifest, insert_clips=[{"media_id": FOOTAGE_B}, {"media_id": FOOTAGE_C}])
    facts = _facts_for_draft(strategy, manifest)
    assert all(row.anchor == "topic" for row in facts.insert_clips)
    verdict = _verdict(facts, _topic_req())
    assert verdict.status == "met"
    assert "end of the sentence" in verdict.reason
    assert "left out" in verdict.reason


def _variant(placed, dropped):
    return plan_facts_from_phone_variant(
        {
            "resolved_archetype": "subtitled",
            "phone_insert_clips": {"placed": placed, "dropped": dropped},
        }
    )


def test_t7_a_finished_video_says_how_many_clips_played_and_why_each_missing_one_did_not():
    facts = _variant(
        [{"media_id": "a", "basis": "topic"}, {"media_id": "b", "basis": "topic"}],
        [{"media_id": "c", "reason": "no_matching_sentence"}],
    )
    assert facts.insert_clips == (
        InsertClipFact("applied"),
        InsertClipFact("applied"),
        InsertClipFact("dropped", reason="no_matching_sentence"),
    )
    verdict = _verdict(facts, _topic_req())
    assert verdict.status == "partial"
    assert "2 of 3" in verdict.reason
    assert "none of what you say is about" in verdict.reason


def test_t7_every_reason_the_topic_path_can_write_has_english_and_turkish_wording():
    from app.kria import brief_checks as bc

    generic = bc._insert_clip_why("zzz")
    for reason in (
        "no_matching_sentence",
        "low_confidence",
        "topic_check_unavailable",
        "in_tail_window",
        "in_hook_window",
        "too_close_to_previous",
    ):
        assert bc._insert_clip_why(reason) != generic, reason
    assert "last" in bc._insert_clip_why("in_tail_window")
    from app.kria.reply_language import reply_language_for

    with reply_language_for("tr"):
        assert bc._insert_clip_why("no_matching_sentence") != bc._insert_clip_why("zzz")
