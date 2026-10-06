"""A Talking edit's first draft is not refused over what the format does.

Thread 17f666cb (2026-10-06, "3 sourdough mistakes"): "Cut out the long pauses, the
part where I say 'let me start that one again', and the 'where was I' bit. Karaoke
captions with the key words highlighted. Add a hook title in the first 2 seconds.
Keep it under 45 seconds." The brief filed the cut sentence as `timing` with
``duration_s: 45``; `_check_timing` judged it "Partly: a Talking edit keeps your
whole take", and the KRI-459 gate threw the draft away with "Your current draft is
unchanged. Should I try a different approach, or make this simpler version?" on a
thread that had no draft. Karaoke captions were "Couldn't verify" with no checker.

Now a speech-cleanup ask and a captions ask have checkers, and a receipt that only
describes what the chosen format does (`is_format_limit`) is an honest "Partly"
that never turns into the simplification question (`needs_creator_choice`).
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    is_format_limit,
    is_judged,
    needs_creator_choice,
    plan_facts_from_strategy,
    reply_from_receipts,
)
from app.kria.contracts import RequirementReceipt

_SOURDOUGH_CUTS = (
    "Cut out the long pauses, the part where I say 'let me start that one again', "
    "and the 'where was I' bit."
)


def _req(kind: str, description: str, **facts) -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind=kind, scope="global", description=description, facts=facts
    )


def _talking(**changes) -> PlanFacts:
    return PlanFacts(
        **{
            "edit_format": "subtitled",
            "caption_style": "karaoke",
            "speech_cleanup_offered": True,
            **changes,
        }
    )


# --- the sourdough turn ------------------------------------------------------------------


def test_the_sourdough_cut_request_is_an_honest_partly_that_never_blocks_the_draft():
    req = _req("timing", _SOURDOUGH_CUTS, duration_s=45)

    receipt = check_requirement(req, _talking())

    assert receipt.status == "partial"
    assert receipt.reason == (
        "Choose Clean up speech when you approve and the long pauses are cut; "
        "a retake or a specific line isn't cut automatically yet, so trim that in the "
        "editor; the length follows what's left of your take, so I can't promise 45s"
    )
    assert is_judged(req, receipt)
    [bound] = build_receipts([req], _talking(), include_unchecked=True)
    assert bound.verification == "checked"
    assert not needs_creator_choice(bound)


def test_karaoke_captions_are_done_on_a_karaoke_draft():
    req = _req("style", "Karaoke captions with the key words highlighted")

    receipt = check_requirement(req, _talking())

    assert receipt.status == "met"
    assert receipt.reason == "words light up as you say them"
    assert is_judged(req, receipt)


def test_the_sourdough_reply_makes_the_draft_and_says_what_the_format_does():
    cuts = _req("timing", _SOURDOUGH_CUTS, duration_s=45)
    karaoke = BriefRequirement(
        id="r2",
        kind="style",
        scope="global",
        description="Karaoke captions with the key words highlighted",
    )
    title = BriefRequirement(id="r3", kind="text", scope="title", literal="3 sourdough mistakes")
    facts = plan_facts_from_strategy(
        {
            "edit_format": "subtitled",
            "caption_style": "karaoke",
            "opening_title": "3 sourdough mistakes",
            "target_duration_s": 45,
        },
        clip_ids=("clip-1",),
        speech_cleanup_enabled=False,
        speech_cleanup_offered=True,
    )

    receipts = build_receipts([cuts, karaoke, title], facts, include_unchecked=True)
    reply = reply_from_receipts(
        CreativeBrief(version=1, requirements=[cuts, karaoke, title]),
        receipts,
        summary="Here is your Talking edit.",
    )

    assert not any(needs_creator_choice(r) for r in receipts)
    assert reply.startswith("Not everything you asked for made it in:")
    assert "Couldn't verify" not in reply
    assert "- Done: Karaoke captions with the key words highlighted" in reply
    assert '- Done: "3 sourdough mistakes"' in reply
    assert "- Partly: Cut out the long pauses" in reply
    assert "Choose Clean up speech when you approve" in reply
    assert "can't promise 45s" in reply


# --- speech cleanup ----------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["timing", "audio", "style", "select"])
def test_a_pause_cut_ask_is_done_once_speech_cleanup_is_on(kind):
    req = _req(kind, "cut out the long pauses and the ums")

    receipt = check_requirement(req, _talking(speech_cleanup_enabled=True))

    assert receipt.status == "met"
    assert receipt.reason is None


def test_a_pause_cut_ask_waits_for_the_approval_choice():
    req = _req("timing", "remove the long pauses")

    receipt = check_requirement(req, _talking(speech_cleanup_enabled=False))

    assert receipt.status == "partial"
    assert receipt.reason == "Choose Clean up speech when you approve and the long pauses are cut"
    assert is_format_limit(receipt.reason)


def test_a_pause_cut_ask_never_promises_a_choice_it_cannot_see():
    req = _req("timing", "remove the long pauses")

    receipt = check_requirement(req, _talking(speech_cleanup_offered=None))

    assert receipt.status == "partial"
    assert receipt.reason == (
        "If Clean up speech is offered when you approve, choose it to cut the long pauses"
    )
    assert is_format_limit(receipt.reason)


def test_a_pause_cut_ask_says_so_when_cleanup_is_not_offered():
    req = _req("timing", "remove the long pauses")

    receipt = check_requirement(req, _talking(speech_cleanup_offered=False))

    assert receipt.status == "partial"
    assert receipt.reason == (
        "Speech cleanup isn't available for this project yet, so the pauses stay"
    )
    assert is_format_limit(receipt.reason)


def test_a_named_line_is_editor_work_even_with_cleanup_on():
    req = _req("timing", "cut the part where I say 'let me start that one again'")

    receipt = check_requirement(req, _talking(speech_cleanup_enabled=True))

    assert receipt.status == "partial"
    assert receipt.reason == (
        "Speech cleanup cuts the long pauses; a retake or a specific line isn't cut "
        "automatically yet, so trim that in the editor"
    )
    assert is_format_limit(receipt.reason)


def test_a_voiceover_edit_judges_cleanup_the_same_way():
    req = _req("timing", "cut out the pauses")

    receipt = check_requirement(
        req, PlanFacts(edit_format="narrated_planned", speech_cleanup_enabled=True)
    )

    assert receipt.status == "met"


def test_cleanup_words_on_a_montage_take_the_usual_timing_path():
    """Review finding: a montage's length ask must not be swallowed by the cleanup
    checker ("No dead air, 30 seconds max" is still a 30 s ask there)."""
    plain = _req("timing", "cut out the long pauses")
    assert not is_judged(plain, check_requirement(plain, PlanFacts(edit_format="montage")))

    timed = _req("timing", "No dead air, 30 seconds max", duration_s=30)
    receipt = check_requirement(timed, PlanFacts(duration_s=45.0))
    assert receipt.status == "partial"
    assert receipt.reason == "This draft is about 45s; you asked for 30s."


@pytest.mark.parametrize(
    "description",
    [
        "Start it with the hook, keep it 30s",
        "Tighten it up to 30 seconds",
        "Cut the gaps, keep it 30s",
    ],
)
def test_a_length_ask_without_cleanup_words_keeps_the_length_check(description):
    req = _req("timing", description, duration_s=30)

    receipt = check_requirement(req, _talking(speech_cleanup_enabled=True))

    assert "whole take" in (receipt.reason or "")


@pytest.mark.parametrize(
    "description",
    [
        "Add a dramatic pause before the reveal",
        "leave a 1s gap between clips",
        "Try this vibe: cozy",
        "Clean up the colors",
    ],
)
def test_unrelated_asks_are_not_read_as_cleanup(description):
    req = _req("style", description)

    receipt = check_requirement(req, _talking(speech_cleanup_enabled=True))

    assert receipt.status != "met"
    assert not is_judged(req, receipt)


@pytest.mark.parametrize(
    "description",
    [
        "Don't remove the pauses",
        "Keep my natural pauses, I like the rhythm",
        "Leave the silences, don't tighten it",
    ],
)
def test_keeping_the_pauses_is_never_read_as_cleanup(description):
    req = _req("timing", description)

    receipt = check_requirement(req, _talking(speech_cleanup_enabled=True))

    assert receipt.status != "met"
    assert not is_judged(req, receipt)


def test_a_pop_in_ask_that_mentions_a_pause_still_goes_to_the_beat_check():
    req = _req("style", "pop up a sticker when I say pause, cut the rest")

    receipt = check_requirement(req, _talking(speech_cleanup_enabled=True))

    assert "pop-ins" in (receipt.reason or "")


def test_turkish_cleanup_and_caption_asks_match_after_folding():
    cut = _req("timing", "Uzun duraklamaları kes")
    assert check_requirement(cut, _talking(speech_cleanup_enabled=True)).status == "met"

    none = _req("style", "Altyazı olmasın")
    receipt = check_requirement(none, _talking(caption_style="karaoke"))
    assert receipt.status == "partial"
    assert receipt.reason == "Captions are still on in this draft."


def test_cleanup_with_no_format_or_from_the_editor_is_unchecked():
    req = _req("timing", "cut out the long pauses")

    assert not is_judged(req, check_requirement(req, PlanFacts()))
    assert not is_judged(
        req, check_requirement(req, PlanFacts(editor=True, edit_format="subtitled"))
    )


def test_keep_my_whole_take_still_goes_to_the_whole_take_check():
    req = _req("timing", "keep the whole take, don't cut anything")

    receipt = check_requirement(req, _talking(video_clip_count=1))

    assert receipt.status == "met"


def test_a_plain_length_on_a_talking_edit_is_partly_but_never_blocks():
    req = _req("timing", "keep it under 45 seconds", duration_s=45)

    receipt = check_requirement(req, _talking())

    assert receipt.status == "partial"
    assert "whole take" in (receipt.reason or "")
    assert is_format_limit(receipt.reason)
    [bound] = build_receipts([req], _talking(), include_unchecked=True)
    assert bound.verification == "checked"
    assert not needs_creator_choice(bound)


def test_a_length_on_a_voiceover_edit_is_partly_but_never_blocks():
    req = _req("timing", "make it 30 seconds", duration_s=30)

    receipt = check_requirement(req, PlanFacts(edit_format="narrated_planned"))

    assert receipt.status == "partial"
    assert is_format_limit(receipt.reason)


# --- captions ----------------------------------------------------------------------------


@pytest.mark.parametrize("style", ["karaoke", "kinetic"])
def test_word_by_word_captions_are_done_on_a_word_style(style):
    req = _req("style", "word-by-word captions, highlight each word")

    receipt = check_requirement(req, _talking(caption_style=style))

    assert receipt.status == "met"


def test_word_by_word_captions_on_sentence_captions_is_a_real_choice():
    req = _req("style", "karaoke captions")

    receipt = check_requirement(req, _talking(caption_style="clean"))

    assert receipt.status == "partial"
    assert receipt.reason == "Captions are on as full sentences, not word by word."
    [bound] = build_receipts([req], _talking(caption_style="clean"), include_unchecked=True)
    assert needs_creator_choice(bound)


@pytest.mark.parametrize(
    "description",
    ["yellow captions at the top", "bigger captions", "captions in Turkish"],
)
def test_a_caption_look_or_language_ask_stays_unchecked(description):
    req = _req("style", description)

    receipt = check_requirement(req, _talking(caption_style="karaoke"))

    assert not is_judged(req, receipt)


def test_auto_captions_cannot_answer_a_word_by_word_or_off_ask():
    word = _req("style", "karaoke captions")
    assert not is_judged(word, check_requirement(word, _talking(caption_style="auto")))
    off = _req("style", "no captions")
    assert not is_judged(off, check_requirement(off, _talking(caption_style="auto")))


def test_plain_captions_are_done_unless_they_are_off():
    req = BriefRequirement(id="r1", kind="text", scope="global", description="Add captions")

    assert check_requirement(req, _talking(caption_style="auto")).status == "met"
    off = check_requirement(req, _talking(caption_style="none"))
    assert off.status == "partial"
    assert off.reason == "Captions are off in this draft."


def test_no_captions_is_done_only_when_they_are_off():
    req = _req("style", "no captions please")

    assert check_requirement(req, _talking(caption_style="none")).status == "met"
    assert check_requirement(req, _talking(caption_style="clean")).status == "partial"


def test_captions_on_a_montage_stay_unchecked_as_before():
    req = _req("style", "add captions")

    receipt = check_requirement(req, PlanFacts(edit_format="montage", caption_style="clean"))

    assert not is_judged(req, receipt)


def test_captions_without_a_strategy_stay_unchecked():
    req = _req("style", "karaoke captions")

    receipt = check_requirement(req, PlanFacts(edit_format="subtitled"))

    assert not is_judged(req, receipt)


def test_a_pop_in_ask_that_mentions_captions_still_goes_to_the_beat_check():
    req = _req("style", "pop up a sticker when I say pasta, and captions")

    receipt = check_requirement(req, _talking())

    # The beat checker's own "can't check" reason, not a captions verdict.
    assert "pop-ins" in (receipt.reason or "")


# --- the gate helper ---------------------------------------------------------------------


def _bound(status: str, reason: str | None, verification: str = "checked") -> RequirementReceipt:
    return RequirementReceipt(
        requirement_id="r1", status=status, reason=reason, verification=verification
    )


def test_needs_creator_choice_reads_dicts_and_receipts_alike():
    assert needs_creator_choice(_bound("partial", "I didn't add a title because of X"))
    assert needs_creator_choice(_bound("not_possible", "No room"))
    assert needs_creator_choice(
        _bound("partial", "Captions are still on in this draft.").model_dump(mode="json")
    )
    assert not needs_creator_choice(_bound("met", None))
    assert not needs_creator_choice(_bound("partial", "anything", verification="unchecked"))
    assert not needs_creator_choice(
        _bound("partial", "A Talking edit keeps your whole take, so its length follows your clip")
    )
    assert not needs_creator_choice(
        _bound("partial", "A voiceover edit runs as long as your voiceover")
    )
