"""KRI-837: a quoted ONE-word end line ("Cheers!") is a cut, and an end not cut is never "met".

Don Julio T1 (kit ``T1_lucas_full_take_talking``, 2026-10-22): one 25.733 s phone take, Lucas the
waiter on camera, Yasin behind the camera, a third voice saying "Cheers!" at 13.8 s, then 10 s of
quiet table chatter. The creator's prompt::

    Talking-to-camera edit of Lucas, our waiter at Don Julio. Start at my "What's your name?"
    and end right after the "Cheers!", drop all the table chatter after it. ... Put a "LUCAS"
    name tag when he says his name, and "CHIMICHURRI" in big letters when he says
    chimichurri. Title: "Sauce check at Don Julio". ... About 15 seconds.

On main the ask reads only the START line: ``opening_cut_ask._fact_line`` and
``take_trim.refile_take_trim`` need ``MIN_QUOTE_TOKENS`` (2) words, and "Cheers!" is one. So

* the draft card says "I'll keep your take from “What's your name?” (0:01) on, if I find that
  line" and the length line says "about 24.8s, you asked for 15s";
* the worker keeps 0.8-25.7 s (the ~10 s of chatter stays) and records only an ``opening_cut``;
* the rendered receipt for the start/end ask is "met", because the end side is not even asked.

The word timings below are the kit's ground truth (``_src/transcript_en.txt``: whisper.cpp
medium forced to English plus a loudness check, +-0.3 s), not a whisper run: the language of the
words the worker is fed is KRI-843 / KRI-844's, this file is about what is done with a
transcript that DOES hold "Cheers!". The matcher already handles it: fed the quote directly,
``apply_take_window`` ends the take at 15.05 s.

How a fix could go wrong, each pinned below (``[guard]`` = already true on main, must stay):

1. The end line is still not read from the brief (draft names one line, worker cuts one side).
2. The ask is read but the draft estimate and the worker disagree (card says 0:15, cut at 25.7).
3. The end is cut, but the receipt still says "met" when the end side was not applied: the line
   is not in the transcript (Turkish "Sagolun" heard), or it is said only BEFORE the start line,
   or an older render recorded no ``end_cut`` at all.
4. A one-word end line matches the wrong "Cheers!": one said before the start line (the search
   must start at the start line), or a later one (the first after the start line ends the video).
5. The cut runs into the next utterance (the end pad stops short of the chatter).
6. Lowering the floor everywhere instead of for the end line: overlay words quoted in the same
   prompt ("LUCAS", "CHIMICHURRI", the title) become cut points [guard], or a route's one-word
   places become take-window lines [guard] (also pinned in ``test_phone_take_window``).
7. Two-word lines stop working or change timings [guard].
8. The legacy ``order`` reply ({"start", "end"}) for one video is not re-filed as the trim
   when the end line is one word, so the model's older shape still asks nothing about it.
9. A legacy ``order`` row (stored before KRI-789, or keyed and left alone by the re-filing)
   keeps the two-word floor on its ``end``, so a one-word end there is no line to cut at; the
   receipt must still not read "met" off the start cut alone, and a route's one-word places
   stay no take window at all [guard].
"""

from __future__ import annotations

import re

import pytest

from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    plan_facts_from_phone_variant,
)
from app.kria.opening_cut_ask import (
    estimate_take_window,
    opening_quote,
    requirement_end_quote,
    requirement_opening_quote,
    take_window_ask,
    wants_take_window,
)
from app.kria.take_trim import refile_take_trim
from app.pipeline.phone_opening_cut import apply_take_window
from app.pipeline.silence_cut import build_cut_plan, no_op_plan, remap_words
from app.tasks import generative_build as gb

# The prompt exactly as the kit has it (T1_lucas_full_take_talking/PROMPT.txt).
PROMPT = (
    'Talking-to-camera edit of Lucas, our waiter at Don Julio. Start at my "What\'s your name?" '
    'and end right after the "Cheers!", drop all the table chatter after it. Captions for '
    "every voice. Spell the sauces right: salsa criolla, chimichurri. I didn't catch the third "
    "sauce he names: if you can't tell what he says, ask me instead of guessing. Put a "
    '"LUCAS" name tag when he says his name, and "CHIMICHURRI" in big letters when he says '
    'chimichurri. Title: "Sauce check at Don Julio". No text on faces. About 15 seconds.'
)
START = "What's your name?"
END = "Cheers!"
TAKE_S = 25.733
# Where the lines are said (kit transcript): "What's" 0.95, "name?" ends 1.7; "Cheers!" 13.8-14.8.
START_WORD_S = 0.95
CHEERS_END_S = 14.8
CHATTER_S = 16.3  # the first Turkish table-chatter word


def _w(text: str, start_s: float, end_s: float) -> dict:
    return {"text": text, "start_s": start_s, "end_s": end_s}


# One word per spoken word, timed to the kit transcript. The chatter after 16 s is what a
# whisper pass hears in the quiet table noise (the kit lists its words as guesses).
WORDS = [
    _w("Tu", 0.0, 0.2), _w("para", 0.2, 0.35), _w("espanol,", 0.35, 0.55), _w("yeah.", 0.55, 0.65),
    _w("What's", 0.95, 1.15), _w("your", 1.15, 1.35), _w("name?", 1.35, 1.7),
    _w("Lucas.", 2.0, 2.5),
    _w("Lucas,", 2.8, 3.1), _w("yeah.", 3.1, 3.6),
    _w("Here", 4.2, 4.4), _w("we", 4.4, 4.5), _w("have", 4.5, 4.7), _w("traditional", 4.7, 5.2),
    _w("Argentinian", 5.2, 5.7), _w("sauces:", 5.7, 6.1),
    _w("salsa", 6.9, 7.3), _w("criolla,", 7.3, 7.9),
    _w("chimichurri", 7.9, 8.8), _w("and", 8.8, 9.0),
    _w("Thank", 10.7, 11.0), _w("you.", 11.0, 11.3),
    _w("Cheers!", 13.8, 14.8),
    _w("Biliyor", 16.3, 16.8), _w("mu?", 16.8, 17.1),
    _w("Evet,", 20.0, 20.4), _w("guzel.", 20.4, 21.0),
]  # fmt: skip
# The take's timed sentences, as the clip record carries them before approval.
SEGMENTS = [
    {"start_s": 0.0, "end_s": 1.7, "text": "Tu para espanol, yeah. What's your name?"},
    {"start_s": 2.0, "end_s": 2.5, "text": "Lucas."},
    {"start_s": 2.8, "end_s": 3.6, "text": "Lucas, yeah."},
    {"start_s": 4.2, "end_s": 6.1, "text": "Here we have traditional Argentinian sauces:"},
    {"start_s": 6.9, "end_s": 7.9, "text": "salsa criolla,"},
    {"start_s": 7.9, "end_s": 9.4, "text": "chimichurri and"},
    {"start_s": 10.7, "end_s": 11.3, "text": "Thank you."},
    {"start_s": 13.8, "end_s": 14.8, "text": "Cheers!"},
    {"start_s": 16.3, "end_s": 17.1, "text": "Biliyor mu?"},
    {"start_s": 20.0, "end_s": 21.0, "text": "Evet, guzel."},
]


def _without_cheers() -> list[dict]:
    """The same take where the third voice is heard as Turkish "Sagolun" (whisper auto-detect)."""
    return [_w("Sagolun", 14.54, 15.0) if w["text"] == END else w for w in WORDS]


def _trim(*, start: str = START, end: str | None = END) -> BriefRequirement:
    """The trim as the Main Creator (prompt v69) records it: ONE ``timing`` requirement."""
    facts = {"start_line": start}
    if end is not None:
        facts["end_line"] = end
    return BriefRequirement(
        id="r2",
        kind="timing",
        scope="global",
        description=f'Start at my "{start}" and end right after the "{end}"',
        facts=facts,
    )


def _length() -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind="timing", scope="global", description="About 15 seconds",
        facts={"duration_s": 15},
    )  # fmt: skip


def _brief(*extra: BriefRequirement) -> list[BriefRequirement]:
    return [_length(), _trim(), *extra]


def _overlays() -> list[BriefRequirement]:
    """The prompt's other quoted words: on-screen text, never cut points."""
    return [
        BriefRequirement(
            id="r3", kind="text", scope="global", literal="LUCAS",
            description='Put a "LUCAS" name tag when he says his name',
            facts={"triggers": ["his name"]},
        ),
        BriefRequirement(
            id="r4", kind="text", scope="global", literal="CHIMICHURRI",
            description='"CHIMICHURRI" in big letters when he says chimichurri',
            facts={"triggers": ["chimichurri"]},
        ),
        BriefRequirement(
            id="r5", kind="text", scope="title", literal="Sauce check at Don Julio",
            description='Title: "Sauce check at Don Julio"',
        ),
        BriefRequirement(
            id="r6", kind="style", scope="global", description="Captions for every voice",
            facts={"captions": "on", "spellings": ["salsa criolla", "chimichurri"]},
        ),
    ]  # fmt: skip


def _snapshot() -> dict:
    return {
        "clip_assignments": [
            {
                "kind": "video",
                "duration_s": TAKE_S,
                "analysis": {
                    "duration_s": TAKE_S,
                    "understanding": {"speech": {"segments": SEGMENTS}},
                },
            }
        ]
    }


def _job(reqs: list[BriefRequirement]) -> dict:
    """The job the worker reads: the approved brief pinned on it (``creator_brief_binding``)."""
    brief = CreativeBrief(version=2, requirements=reqs)
    binding = BriefBinding.create("0d0d0d0d-3737-4837-8837-0123456789ab", brief)
    return {"creator_brief_binding": binding.model_dump(mode="json")}


def _cleanup_plan(words: list[dict]):  # noqa: ANN202
    # What a real cleanup of this take cuts: every pause of 0.6 s or more between words.
    gaps = [
        (a["end_s"], b["start_s"])
        for a, b in zip(words, words[1:], strict=False)
        if b["start_s"] - a["end_s"] >= 0.6
    ]
    return build_cut_plan(words, gaps, TAKE_S)


def _worker(reqs: list[BriefRequirement], words: list[dict] | None = None, *, cleanup: str = "off"):
    """The phone Talking worker's own cut (``_phone_opening_cut``), cleanup off or on."""
    words = WORDS if words is None else words
    if cleanup == "off":
        return gb._phone_opening_cut(_job(reqs), None, (), duration_s=TAKE_S, off_words=words)
    return gb._phone_opening_cut(
        _job(reqs), _cleanup_plan(words), words, duration_s=TAKE_S, off_words=None
    )


def _rendered(**receipts: dict) -> PlanFacts:
    """The finished variant's facts (what a render records) for the receipts."""
    return plan_facts_from_phone_variant(
        {
            "variant_id": "subtitled",
            "resolved_archetype": "subtitled",
            "render_destination": "device",
            "caption_cues": [],
            "text_elements": [],
            "duration_s": 14.3,
            **receipts,
        }
    )


def _receipt_for(reqs: list[BriefRequirement], facts: PlanFacts, req_id: str = "r2"):  # noqa: ANN202
    by_id = {r.requirement_id: r for r in build_receipts(reqs, facts, include_unchecked=True)}
    return by_id[req_id]


def _draft_receipts() -> dict:
    estimate = estimate_take_window(_brief(), _snapshot(), strategy={"media_scope": "selected"})
    facts = PlanFacts(
        edit_format="subtitled", phone_talking=True, take_duration_s=TAKE_S, take_window=estimate
    )
    receipts = build_receipts(_brief(), facts, include_unchecked=True, strict_order=True)
    return {r.requirement_id: r for r in receipts}


# -- 1. the ask: both lines, from the brief the model writes -----------------------------------


def test_the_brief_asks_for_both_lines() -> None:
    ask = take_window_ask(_brief())
    assert ask is not None
    assert (ask.start_quote, ask.end_quote) == (START, END)
    assert ask.start_ids == ask.end_ids == ("r2",)
    assert wants_take_window(_trim())
    assert requirement_end_quote(_trim()) == END


def test_a_one_word_start_line_is_also_a_line() -> None:
    """The other side of the same floor: "Start at "Lucas"" is a quoted line too."""
    ask = take_window_ask([_trim(start="Lucas.", end=None)])
    assert ask is not None and ask.start_quote == "Lucas."


# -- 2. the draft card and the length line ------------------------------------------------------


def test_the_draft_places_both_lines_in_the_take() -> None:
    estimate = estimate_take_window(_brief(), _snapshot(), strategy={"media_scope": "selected"})
    assert estimate is not None
    assert (estimate.start_quote, estimate.end_quote) == (START, END)
    assert estimate.start_s == pytest.approx(START_WORD_S, abs=0.3)
    # "Cheers!" ends 14.8 s; the cut keeps a breath after it, never into the chatter at 16.3 s.
    assert estimate.end_s is not None
    assert CHEERS_END_S <= estimate.end_s <= CHEERS_END_S + 0.5
    assert estimate.kept_s == pytest.approx(14.1, abs=0.5)


def test_the_draft_card_names_both_lines_with_their_times() -> None:
    card = _draft_receipts()["r2"].reason or ""
    assert (
        "I'll keep your take from “What's your name?” (0:01) to right after “Cheers!” (0:1" in card
    )
    # The ticket's "(about 0:14)": the clock is the cut's end, 15.05 s, so 0:14 or 0:15.
    assert re.search(r"right after “Cheers!” \(0:1[45]\), if I find those lines", card), card
    assert "if I find that line" not in card


def test_the_length_line_is_not_the_untrimmed_take() -> None:
    length = _draft_receipts()["r1"].reason or ""
    assert "24.8" not in length and "whole take" not in length, length


# -- 3. the worker: 0.8-15.0 s of the 25.733 s take ----------------------------------------------


@pytest.mark.parametrize("cleanup", ["off", "on"])
def test_the_worker_cuts_both_sides(cleanup: str) -> None:
    plan, receipts = _worker(_brief(), cleanup=cleanup)

    assert receipts is not None and set(receipts) == {"opening_cut", "end_cut"}, receipts
    assert {r["status"] for r in receipts.values()} == {"applied"}
    assert receipts["end_cut"]["quote"] == END
    assert receipts["end_cut"]["requirement_ids"] == ["r2"]
    assert plan is not None
    assert plan.keep_segments[0][0] == pytest.approx(0.8, abs=0.3)
    end_s = plan.keep_segments[-1][1]
    assert end_s == pytest.approx(15.0, abs=0.3)
    # Within half a second after "Cheers!" ends, and well before the chatter starts.
    assert CHEERS_END_S <= end_s <= CHEERS_END_S + 0.5 < CHATTER_S
    said = [w["text"] for w in remap_words(WORDS, plan)]
    assert said[0] == "What's" and said[-1] == END
    assert "Biliyor" not in said and "Evet," not in said


def test_a_take_that_already_ends_on_cheers_needs_no_end_cut() -> None:
    """A take that already ends on "Cheers!" keeps its end: the ask is applied, 0 s removed."""
    words = [w for w in WORDS if w["start_s"] < CHATTER_S]
    plan, receipts = gb._phone_opening_cut(
        _job(_brief()), None, (), duration_s=14.8, off_words=words
    )
    assert receipts is not None and receipts["end_cut"]["status"] == "applied"
    assert plan is not None and plan.keep_segments[-1][1] == pytest.approx(14.8, abs=0.01)


# -- 4. the receipt: "met" only when both cuts were applied ---------------------------------------


def test_the_rendered_receipt_is_met_because_both_cuts_were_applied() -> None:
    _plan, receipts = _worker([_length(), _trim()])
    assert receipts is not None and set(receipts) == {"opening_cut", "end_cut"}, receipts
    assert {r["status"] for r in receipts.values()} == {"applied"}

    receipt = _receipt_for([_length(), _trim()], _rendered(**receipts))

    assert (receipt.status, receipt.reason) == ("met", None)


def test_a_render_that_recorded_only_the_start_cannot_certify_the_end() -> None:
    """What the worker on main leaves for this ask: an ``opening_cut`` and no ``end_cut``."""
    start_only = {
        "opening_cut": {
            "version": 1,
            "quote": START,
            "requirement_ids": ["r2"],
            "status": "applied",
            "start_s": 0.8,
            "removed_s": 0.8,
            "exact": True,
        }  # fmt: skip
    }

    receipt = _receipt_for([_length(), _trim()], _rendered(**start_only))

    assert receipt.status != "met", receipt
    assert receipt.verification == "unchecked"


@pytest.mark.parametrize("words", ["no_cheers", "cheers_only_before_the_start"])
def test_an_end_line_that_was_not_applied_is_never_met(words: str) -> None:
    if words == "no_cheers":
        spoken, reason = _without_cheers(), "not_found"
    else:
        # The third voice says it once, before the start line; never after it.
        spoken = [_w(END, 0.0, 0.4) if w["text"] == END else w for w in WORDS]
        spoken.sort(key=lambda w: w["start_s"])
        reason = "before_start"
    reqs = [_length(), _trim()]

    plan, receipts = _worker(reqs, spoken)

    assert receipts is not None and set(receipts) == {"opening_cut", "end_cut"}, receipts
    assert receipts["opening_cut"]["status"] == "applied"
    assert (receipts["end_cut"]["status"], receipts["end_cut"]["reason"]) == ("not_applied", reason)
    # Only the start was cut: the take runs to its end, and nothing claims otherwise.
    assert plan is not None and plan.keep_segments[-1][1] == pytest.approx(TAKE_S)
    receipt = _receipt_for(reqs, _rendered(**receipts))
    assert receipt.status == "partial", receipt
    assert "“Cheers!”" in (receipt.reason or "")


@pytest.mark.parametrize("unreadable", ["\U0001f942", "!!!"])
def test_an_end_line_with_no_words_in_it_is_never_certified_by_the_start_alone(
    unreadable: str,
) -> None:
    """A stored end line the reader cannot use (no words to match) is still an end ask: with the
    start cut applied, the receipt must not read "met" off the start side alone."""
    reqs = [_length(), _trim(end=unreadable)]
    assert requirement_end_quote(reqs[1]) is None  # nothing to cut at on this side

    plan, receipts = _worker(reqs)

    assert receipts is not None and set(receipts) == {"opening_cut"}, receipts
    assert plan is not None and plan.keep_segments[-1][1] == pytest.approx(TAKE_S)
    receipt = _receipt_for(reqs, _rendered(**receipts))
    assert receipt.status != "met", receipt
    assert receipt.verification == "unchecked"


def _legacy_order(*, key: str | None = None, **facts: str) -> BriefRequirement:
    """The pre-KRI-789 shape (and a keyed reply refile leaves alone): ``order`` + start / end."""
    return BriefRequirement(
        id="r2",
        kind="order",
        scope="global",
        description='Start at my "What\'s your name?" and end right after the "Cheers!"',
        facts={**({"key": key} if key else {}), **({"start": START, "end": END} | facts)},
    )


@pytest.mark.parametrize("key", [None, "capture_time"])
def test_a_legacy_order_row_with_a_one_word_end_is_never_certified_by_the_start_alone(
    key: str | None,
) -> None:
    """The legacy ``end`` keeps the two-word floor (it is a route's place on a keyed row), so
    "Cheers!" there is not a line to cut at; the end is still asked, so the start cut applied
    must not read "met" for the whole ask."""
    reqs = [_length(), _legacy_order(key=key)]
    assert requirement_opening_quote(reqs[1]) == START
    assert requirement_end_quote(reqs[1]) is None

    plan, receipts = _worker(reqs)

    assert receipts is not None and set(receipts) == {"opening_cut"}, receipts
    assert plan is not None and plan.keep_segments[-1][1] == pytest.approx(TAKE_S)
    receipt = _receipt_for(reqs, _rendered(**receipts))
    assert receipt.status != "met", receipt
    assert receipt.verification == "unchecked"


def test_a_legacy_order_row_with_a_two_word_end_is_still_cut_and_met() -> None:
    """[guard] The same legacy shape with a two-word end ("Thank you.") reads both lines."""
    reqs = [_length(), _legacy_order(end="Thank you.")]

    plan, receipts = _worker(reqs)

    assert receipts is not None and {r["status"] for r in receipts.values()} == {"applied"}
    assert set(receipts) == {"opening_cut", "end_cut"}
    receipt = _receipt_for(reqs, _rendered(**receipts))
    assert (receipt.status, receipt.reason) == ("met", None)


@pytest.mark.parametrize("key", [None, "capture_time"])
def test_a_route_row_with_one_word_places_is_still_no_take_window(key: str | None) -> None:
    """[guard] Arnavutkoy -> Eminonu: one word each, so no line on either side. The row is not
    judged as a take window, whatever the render recorded about the take."""
    route = _legacy_order(key=key, start="Arnavutkoy", end="Eminonu")
    route = route.model_copy(update={"description": "Start in Arnavutkoy and finish in Eminonu"})

    assert take_window_ask([route]) is None
    assert not wants_take_window(route)
    plain = _receipt_for([route], _rendered())
    cut = {"opening_cut": {"status": "applied", "quote": START, "requirement_ids": ["r2"]}}
    assert _receipt_for([route], _rendered(**cut)) == plain


# -- 5. the right "Cheers!" ----------------------------------------------------------------------


def test_the_end_line_is_the_first_one_after_the_start_line() -> None:
    """One said before the start line, one at 13.8 s, one more at 22 s: the video ends at 13.8."""
    spoken = sorted([_w(END, 0.0, 0.4), *WORDS, _w(END, 22.0, 22.6)], key=lambda w: w["start_s"])

    plan, receipts = _worker([_length(), _trim()], spoken)

    assert receipts is not None and receipts["end_cut"]["status"] == "applied"
    assert plan is not None
    assert plan.keep_segments[-1][1] == pytest.approx(15.0, abs=0.3)
    assert plan.keep_segments[0][0] == pytest.approx(0.8, abs=0.3)


# -- 6. the floor stays where it is for everything that is not an end line ----------------------


def test_overlay_words_in_the_same_prompt_are_never_cut_points() -> None:
    """On-screen words ("LUCAS", "CHIMICHURRI", the title): no row but the trim holds a line."""
    for req in _overlays():
        assert requirement_opening_quote(req) is None, req.id
        assert requirement_end_quote(req) is None, req.id
        assert not wants_take_window(req), req.id


def test_the_whole_brief_still_asks_only_for_the_two_spoken_lines() -> None:
    """With the overlay rows beside the trim, the ask is still exactly the two lines."""
    ask = take_window_ask(_brief(*_overlays()))
    assert ask is not None
    assert (ask.start_quote, ask.end_quote) == (START, END)
    assert ask.start_ids == ask.end_ids == ("r2",)


def test_a_prompt_in_one_description_still_reads_only_the_start_cue() -> None:
    """No facts, the whole prompt as the description: the "start" cue picks the one quote beside
    it, never "Cheers!", "LUCAS", "CHIMICHURRI" or the title; the end line comes only from a
    field (KRI-667: no new reading of the words)."""
    described = BriefRequirement(id="r2", kind="select", scope="global", description=PROMPT[:300])
    assert opening_quote(PROMPT) == START
    assert requirement_opening_quote(described) == START
    assert requirement_end_quote(described) is None


def test_a_one_word_line_is_exact_never_fuzzy() -> None:
    """ "Cheers" is not "Cheer", "Cheese" or "Thank you": a one-word line only matches itself."""
    for heard in ("Cheese!", "Cheer", "Chairs", "Sagolun"):
        spoken = [_w(heard, 13.8, 14.8) if w["text"] == END else w for w in WORDS]
        _plan, _start, end = apply_take_window(
            no_op_plan(TAKE_S), spoken, start_quote=START, end_quote=END, duration_s=TAKE_S
        )
        assert (end["status"], end["reason"]) == ("not_applied", "not_found"), heard


@pytest.mark.parametrize("heard", ["cheers", "Cheers.", "CHEERS!", "Cheers,"])
def test_case_and_punctuation_do_not_matter_to_a_one_word_line(heard: str) -> None:
    spoken = [_w(heard, 13.8, 14.8) if w["text"] == END else w for w in WORDS]

    _plan, _start, end = apply_take_window(
        no_op_plan(TAKE_S), spoken, start_quote=START, end_quote=END, duration_s=TAKE_S
    )

    assert end["status"] == "applied" and end["end_s"] == pytest.approx(15.05, abs=0.01)


# -- 7. two-word lines behave exactly as before ---------------------------------------------------


def test_a_two_word_end_line_still_cuts_after_it() -> None:
    reqs = [_length(), _trim(end="Thank you.")]
    ask = take_window_ask(reqs)
    assert ask is not None and ask.end_quote == "Thank you."

    plan, receipts = _worker(reqs)

    assert receipts is not None and {r["status"] for r in receipts.values()} == {"applied"}
    assert plan is not None
    assert plan.keep_segments[0][0] == pytest.approx(0.8, abs=0.3)
    assert plan.keep_segments[-1][1] == pytest.approx(11.55, abs=0.3)  # "you." ends 11.3 s
    receipt = _receipt_for(reqs, _rendered(**receipts))
    assert (receipt.status, receipt.reason) == ("met", None)


def test_the_matcher_itself_ends_the_take_at_cheers() -> None:
    """The ticket's claim: fed "Cheers!" directly, the matcher ends at about 15.05 s."""
    plan, start, end = apply_take_window(
        no_op_plan(TAKE_S), WORDS, start_quote=START, end_quote=END, duration_s=TAKE_S
    )
    assert start["status"] == end["status"] == "applied"
    assert plan.keep_segments == [(pytest.approx(0.8, abs=0.01), pytest.approx(15.05, abs=0.01))]


# -- 8. the older reply shape for one video -------------------------------------------------------


def _order_update(**facts: object) -> BriefUpdate:
    return BriefUpdate(
        kind="order",
        scope="global",
        description='Start at my "What\'s your name?" and end right after the "Cheers!"',
        facts={"start": START, "end": END, **facts},
    )


def test_a_v64_order_reply_with_a_one_word_end_is_refiled_as_the_trim() -> None:
    """The model's older shape (key-less ``order`` with start / end) on a one-video edit."""
    refiled = refile_take_trim(_order_update(), creator_message=PROMPT)

    assert refiled.kind == "timing"
    assert refiled.facts == {"start_line": START, "end_line": END}


def test_quoted_route_places_are_not_refiled_as_a_trim() -> None:
    """One word each, quoted, and a capture-time order basis: places, not lines."""
    message = 'I ran from "Arnavutkoy" to "Eminonu", oldest clip first'
    update = BriefUpdate(
        kind="order",
        scope="global",
        description="Start in Arnavutkoy and finish in Eminonu",
        facts={"key": "capture_time", "start": "Arnavutkoy", "end": "Eminonu"},
    )

    assert refile_take_trim(update, creator_message=message) is update
