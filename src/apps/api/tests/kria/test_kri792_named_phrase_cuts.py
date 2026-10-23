"""KRI-792: a named spoken stretch to cut is never "Done" before anything cuts it.

Prod (café kit T1 Talking, 2026-10-21, thread 32A5B608, job f88555bc on 042c405c9): the
creator wrote Cut her slip "with bread, no," and Also cut my half question "how are you
gonna". The brief stored both as 'single-quoted' descriptions carrying only
{"speech_cleanup": true}. Once Clean up speech was chosen, both receipts turned "met" and the
approval card said "Done: Cut her slip ...". Speech cleanup cuts long pauses and filler
sounds; it never cuts a spoken line, so the render would have kept both.

Failure modes, written before the fix:
1. The brief writer swaps the creator's "..." for '...' and the quoted line is missed, so the
   receipt is "met" once cleanup is on (the prod rows, replayed below from the fixture).
2. The stretch is named with a word, not a quote ("cut her slip", "the half question", "my
   mistake", "the stumble", "the bit about ..."; Turkish "dil sürçmesini kes", "yarım kalan
   soruyu çıkar"), and it is missed.
3. An apostrophe inside a word (I'm, Karolina's, Atakan'ın, the curly I’m iOS types) is read
   as a quote mark, so a plain pause cut gets a bogus "stays in your video" line.
4. The model records the line in `facts.cut_lines`, the description has neither quotes nor
   named-cut words, and the fact is ignored.
5. `cut_lines` is empty, a bare string or holds non-strings: a crash or a wrong verdict.
6. A `cut_lines` item that is only a filler sound ("um") is reported as a line that stays in.
7. A named cut reads "met" in any cleanup state before render (on, offered, unavailable,
   unknown) or after it (cleanup applied, or found nothing to cut).
8. `cut_lines` on an ask with no `speech_cleanup` fact and no cleanup words reads "met".
   (It is not routed to the cleanup checker: that would also make the take-length question
   wait for a cleanup that won't shorten anything, `choice_questions._asks_speech_cleanup`.)
9. An explicit {"speech_cleanup": false} ("keep my pauses, but cut ...") is judged as a
   pause-cleanup ask because it also names a line.
10. The honest "Partly" asks the "simpler version?" question. It is a limit of the format
    and must never hold the draft back.
11. "Captions with no spelling mistakes" or "drop the slippers shot" turns into a
    speech-cleanup ask: a named-cut word counts only whole and right after a cut verb.
12. A Turkish chat gets the English note.
(Pop-in trigger words keep reading double quotes only: the shared `_QUOTED_RE` is unchanged,
and only `_wants_named_cuts` reads single quote marks.)
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from app.agents.main_creator import MainCreatorAgent
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    is_format_limit,
    needs_creator_choice,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
    reply_from_receipts,
    speech_cleanup_review_note,
)
from app.kria.reply_language import reply_language_for

_FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri792_t1_phrase_cuts.json").read_text()
)
_STAYS_NOTE = "a retake or a specific line can't be cut out yet, so it stays in your video"
_STAYS_NOTE_TR = "tekrar çekim ya da belirli bir cümle henüz kesilemiyor, o yüzden videoda kalıyor"
_SLIP = "Cut her slip 'with bread, no,' so it says 'waffle cookie with blackberry jam'"
_HALF_QUESTION = "Cut the half question 'how are you gonna'"


def _prod_brief(**fact_changes: dict[str, Any]) -> CreativeBrief:
    """The job's bound brief, optionally with extra facts on some rows (by id)."""
    rows = [
        {**row, "facts": {**row["facts"], **fact_changes.get(row["id"], {})}}
        for row in _FIXTURE["brief"]["requirements"]
    ]
    return CreativeBrief.model_validate({**_FIXTURE["brief"], "requirements": rows})


def _draft_facts(**cleanup: bool | None) -> PlanFacts:
    """What the runtime judges the draft with (`tasks/kria_runtime.py`, KRI-705 phone Talking)."""
    strategy = _FIXTURE["strategy"]
    facts = plan_facts_from_strategy(strategy, clip_ids=strategy["selected_media_ids"], **cleanup)
    return dataclasses.replace(facts, phone_talking=True)


def _by_id(brief: CreativeBrief, facts: PlanFacts) -> dict[str, Any]:
    return {
        r.requirement_id: r for r in build_receipts(brief.live(), facts, include_unchecked=True)
    }


def _req(kind: str, description: str, **facts: Any) -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind=kind, scope="global", description=description, facts=facts
    )


def _talking(**changes: Any) -> PlanFacts:
    return PlanFacts(
        **{"edit_format": "subtitled", "caption_style": "karaoke", "speech_cleanup_enabled": True}
        | changes
    )


def _assert_stays_in(receipt: Any) -> None:
    assert receipt.status == "partial", receipt
    assert _STAYS_NOTE in (receipt.reason or "")
    assert is_format_limit(receipt.reason)  # 10: never the "simpler version?" question
    assert not needs_creator_choice(receipt)


# --- 1 + 7 + 10: the prod rows, replayed -----------------------------------------------------


def test_the_fixture_is_the_prod_turn_that_said_done():
    assert {"r3", "r4"} <= {r["id"] for r in _FIXTURE["brief"]["requirements"]}
    descriptions = {r["id"]: r["description"] for r in _FIXTURE["brief"]["requirements"]}
    assert (descriptions["r3"], descriptions["r4"]) == (_SLIP, _HALF_QUESTION)
    assert _FIXTURE["prod_receipts"]["turn4_cleanup_on"]["r3"]["status"] == "met"
    assert f"Done: {_SLIP}" in _FIXTURE["prod_approval_card"]


@pytest.mark.parametrize(
    "cleanup",
    [
        pytest.param({"speech_cleanup_enabled": True, "speech_cleanup_offered": True}, id="on"),
        pytest.param({"speech_cleanup_offered": True}, id="offered"),
        pytest.param({"speech_cleanup_offered": False}, id="unavailable"),
        pytest.param({}, id="unknown"),
    ],
)
def test_the_prod_slip_and_half_question_stay_in_every_cleanup_state(cleanup):
    receipts = _by_id(_prod_brief(), _draft_facts(**cleanup))
    _assert_stays_in(receipts["r3"])
    _assert_stays_in(receipts["r4"])
    # The title the same draft honoured is still "met": only the cut asks changed.
    assert receipts["r6"].status == "met"


def test_the_prod_approval_card_no_longer_says_done_for_the_cuts():
    brief = _prod_brief()
    receipts = build_receipts(
        brief.live(),
        _draft_facts(speech_cleanup_enabled=True, speech_cleanup_offered=True),
        include_unchecked=True,
    )
    card = reply_from_receipts(brief, receipts)
    assert f"Done: {_SLIP}" not in card and f"Done: {_HALF_QUESTION}" not in card
    assert f"Partly: {_SLIP} (Speech cleanup cuts the long pauses; {_STAYS_NOTE})" in card
    assert f"Partly: {_HALF_QUESTION} (Speech cleanup cuts the long pauses; {_STAYS_NOTE})" in card


@pytest.mark.parametrize("outcome", ["applied", "no_change"])
def test_after_the_render_the_cut_asks_stay_in(outcome):
    variant = {
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "caption_cues": [{"text": "Hi, I'm Karolina", "start_s": 0.0, "end_s": 1.4}],
        "voiceover_caption_style": "sentence",
        "silence_cut_outcome": outcome,
        "silence_cut": {
            "removed": [{"start_s": 3.0, "end_s": 4.1, "reason": "silence"}],
            "time_saved_s": 1.1,
        },
    }
    brief = _prod_brief()
    receipts = _by_id(brief, plan_facts_from_phone_variant(variant))
    _assert_stays_in(receipts["r3"])
    _assert_stays_in(receipts["r4"])
    # The review note does not repeat the limit the receipts already state.
    reply = reply_from_receipts(brief, list(receipts.values()))
    note = speech_cleanup_review_note(variant, text=reply) or ""
    assert "sentence you restarted" not in note


# --- 4: the v65 brief records the lines; the fact decides -----------------------------------


def test_a_recorded_cut_line_decides_when_the_words_name_no_cut():
    described = _req("audio", "Remove what she says about the bread", speech_cleanup=True)
    assert check_requirement(described, _talking()).status == "met"  # the words alone miss it
    recorded = _req(
        "audio",
        "Remove what she says about the bread",
        speech_cleanup=True,
        cut_lines=["with bread, no,"],
    )
    _assert_stays_in(check_requirement(recorded, _talking()))


def test_the_prod_rows_with_the_v65_fact_stay_in_too():
    brief = _prod_brief(
        r3={"cut_lines": ["with bread, no,"]}, r4={"cut_lines": ["how are you gonna"]}
    )
    receipts = _by_id(brief, _draft_facts(speech_cleanup_enabled=True, speech_cleanup_offered=True))
    _assert_stays_in(receipts["r3"])
    _assert_stays_in(receipts["r4"])


def test_both_brief_prompts_show_a_slip_as_a_cut_line():
    from app.agents._schemas.brief_extractor import BriefExtractionInput
    from app.agents.brief_extractor import BriefExtractorAgent
    from tests.agents.test_main_creator_agent import _input

    planner = MainCreatorAgent(None).render_prompt(  # type: ignore[arg-type]
        _input().model_copy(update={"brief_enabled": True})
    )
    extractor = BriefExtractorAgent(None).render_prompt(  # type: ignore[arg-type]
        BriefExtractionInput(creator_request="", user_message="x", conversation=[])
    )
    example = '{"speech_cleanup": true, "cut_lines": ["with bread, no,"]}'
    assert example in planner and example in extractor


# --- 2: a stretch named with a word ----------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "Cut her slip",
        "Also cut my half question",
        "Cut out my mistake at the start",
        "Remove the stumble in the middle",
        "Cut the bit about the bread",
        "Cut her slip “with bread, no,”",
        "Dil sürçmesini kes",
        "Yarım kalan soruyu çıkar",
        "Baştaki hatayı kes",
    ],
)
def test_a_stretch_named_with_a_word_stays_in(description):
    _assert_stays_in(check_requirement(_req("audio", description), _talking()))


# --- 1 + 3: single quote marks vs apostrophes ------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "Cut 'how are you gonna' and the long pauses",
        "Cut the long pauses and ‘how are you gonna’",
        "Cut the long pauses and 'I'm gonna be' at the end",
    ],
)
def test_a_single_quoted_line_stays_in(description):
    _assert_stays_in(check_requirement(_req("audio", description, speech_cleanup=True), _talking()))


@pytest.mark.parametrize(
    "description",
    [
        "Cut the long pauses, I'm slow to get going",
        "Cut Karolina's long pauses and the ums",
        "Cut the long pauses, I’m slow and Karolina’s take drags",
        "Atakan'ın uzun duraklamalarını kes",
        "Cut the pauses and the 'um's",
    ],
)
def test_an_apostrophe_in_a_word_is_not_a_quoted_line(description):
    receipt = check_requirement(_req("audio", description, speech_cleanup=True), _talking())
    assert receipt.status == "met", receipt


# --- 5 + 6: odd cut_lines values -------------------------------------------------------------


@pytest.mark.parametrize("value", [[], "", [3, None], ["um"], ["Uh."], {"line": "x"}])
def test_an_empty_or_filler_cut_line_falls_back_to_the_words(value):
    receipt = check_requirement(
        _req("audio", "Cut the long pauses", speech_cleanup=True, cut_lines=value), _talking()
    )
    assert receipt.status == "met", receipt


@pytest.mark.parametrize(
    "value",
    [
        "how are you gonna",
        ["the whole bit where I explain how the waffle cookie is made, start to finish, twice"],
    ],
)
def test_a_bare_string_or_a_long_cut_line_is_a_named_cut(value):
    receipt = check_requirement(
        _req("audio", "Cut the long pauses", speech_cleanup=True, cut_lines=value), _talking()
    )
    _assert_stays_in(receipt)


# --- 8 + 9: routing --------------------------------------------------------------------------


def test_a_cut_line_without_a_cleanup_fact_is_never_done():
    req = _req("audio", "Drop how are you gonna", cut_lines=["how are you gonna"])
    assert check_requirement(req, _talking()).status != "met"
    # On a phone Talking draft it is a plain "can't" before approval (KRI-705).
    (receipt,) = build_receipts([req], _talking(phone_talking=True), include_unchecked=True)
    assert receipt.status == "not_possible"


def test_keep_my_pauses_with_a_cut_line_is_not_a_pause_cleanup_ask():
    receipt = check_requirement(
        _req(
            "audio",
            "Keep my pauses, just drop how are you gonna",
            speech_cleanup=False,
            cut_lines=["how are you gonna"],
        ),
        _talking(),
    )
    assert receipt.status != "met"
    assert "Speech cleanup" not in (receipt.reason or "")


# --- 11: "mistakes" in a captions ask, "slippers" in a shot ask ------------------------------


@pytest.mark.parametrize(
    ("req", "status"),
    [
        (_req("style", "Captions with no spelling mistakes", captions="on"), None),
        (_req("audio", "Cut the long pauses and make sure there are no mistakes"), "met"),
        (_req("select", "Drop the slippers shot"), None),
    ],
)
def test_no_mistakes_is_not_a_named_cut(req, status):
    receipt = check_requirement(req, _talking())
    assert _STAYS_NOTE not in (receipt.reason or "")
    assert "Speech cleanup" not in (receipt.reason or "") or status == "met"
    if status:
        assert receipt.status == status


# --- 12: Turkish -----------------------------------------------------------------------------


def test_a_turkish_chat_gets_the_turkish_stays_in_note():
    req = _req("audio", "Dil sürçmesini kes", speech_cleanup=True, cut_lines=["ekmekle, yok,"])
    with reply_language_for("tr"):
        receipt = check_requirement(req, _talking())
    assert receipt.status == "partial"
    assert _STAYS_NOTE_TR in (receipt.reason or "")
