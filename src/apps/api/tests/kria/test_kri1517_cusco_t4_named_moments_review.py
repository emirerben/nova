"""Cusco DJI T4 "Altının yolu": named moments on a Voiceover, judged on the finished render.

Phone Voiceover job 91270ec0 (2026-11-17, build 2016, server 80c96b04c): a 63.3 s Turkish voiceover
file over 11 silent clips. The creator asked for four clips on four quoted phrases ("the paintings
in gold frames on "arkadaşlar İspanyollar geldi"", "the dark stone wall with the window on
"İnkalar için"", "the snowy mountains on "Ant Dağları'ndan"", "the wide view of the city on
"Osmanlı'ya dayanıyor""). Clean up speech then cut 6.2 s as a restarted sentence (KRI-1518, since
fixed in #2378), and "Ant Dağları'ndan" was gone from the voice. The review still listed all four
moments under "Have a look at these in the video" and never said the words were missing.

The brief typed these rows as ``kind: style``, ``scope: global`` with ``facts.triggers``: the clip
is named by the creator's own words for it, never by a clip id. The worker stored those words on
each step it placed (``narrated_alignment.steps[*].labels``). The Tahini check (`check_clip_cue`)
judged only ``clip:<id>``-scoped rows, so these four stayed unchecked.

``tests/fixtures/narrated_alignment/kri1517_cusco_t4_moments.json`` is the job's own record: the
approved brief rows, the render's steps (media renamed c0..c10), the variant (no URL or storage
path) and the review's recorded receipts. Read-only admin GET, no media, no model call.

The run's notes also called the wall clip 2.2 s late (a comment on KRI-1506). Measured again on
the kit audio for this ticket, that was a misreading: the voice-band level and a separate local
transcription of each sound burst put "takılar" at 15.32-15.69 s (source), an unvoiced breath at
16.7-17.45 s (the voicing detector finds no voice there; alone it transcribes as the usual
"Altyazı M.K." hallucination), and "İnkalar için altın kutsal bir şeydi" at 17.64-19.71 s. In the
finished video "İnkalar" starts at about 15.9 s and the wall comes on at 15.96 s. So r4 is met.

Ways this can fail, written before the code:

 F1 the replay is not the incident: with the receipts switch off, r3-r6 are not the recorded
    "partial / unchecked" (red control), so a pass proves nothing.
 F2 a moment whose words are in the finished voice and whose clip is on screen on them is not
    "Done" with its times (r3 paintings, r4 wall, r6 city); or the phrase that spans two steps
    ("arkadaşlar | İspanyollar geldi") is judged on the wrong step.
 F3 a moment whose words are not in the finished voice (r5, cut by cleanup) still reads "Have a
    look", or is called "Done", or is blamed on a clip; it must say the words were not found in
    the finished voiceover and where the named clip plays instead.
 F4 wrong content passes: the same render judged with the wall label on another clip's step, or
    an ask naming a clip label no step carries, is "Done".
 F5 a row that names no step label gets a verdict it cannot have: "Fill the rest with the other
    clips" (no spoken words) and a word cue naming no shot keep their old answer.
 F6 a label hidden inside a longer one, or a generic label ("the other clips"), claims a row
    that only shares a word with it.
 F7 a draft (no steps yet) is newly judged; the Tahini clip-scoped cue and pace answers change.
 F8 Turkish folding: "Osmanlı'ya" in another step's words ("Osmanlı'ya bağlanıyor") is taken as
    "Osmanlı'ya dayanıyor".
 F9 the kill switch (NARRATED_CUE_RECEIPTS_ENABLED off) no longer restores the old answer.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    check_requirement,
    is_judged,
    plan_facts_from_narrated_alignment,
    plan_facts_from_phone_variant,
)

CASE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "narrated_alignment"
        / "kri1517_cusco_t4_moments.json"
    ).read_text(encoding="utf-8")
)
REQUIREMENTS = {row["id"]: BriefRequirement.model_validate(row) for row in CASE["requirements"]}
RECORDED = {row["requirement_id"]: row for row in CASE["review_event_27"]}
MOMENTS = ("r3", "r4", "r5", "r6")


def _facts(steps: list[dict] | None = None) -> PlanFacts:
    record = dict(CASE["narrated_alignment"])
    if steps is not None:
        record["steps"] = steps
    return dataclasses.replace(
        plan_facts_from_phone_variant(CASE["variant"]),
        narrated_cue_steps=plan_facts_from_narrated_alignment(record).narrated_steps,
    )


def _judge(rid: str, facts: PlanFacts | None = None, req: BriefRequirement | None = None):
    return check_requirement(req or REQUIREMENTS[rid], facts or _facts())


@pytest.fixture(autouse=True)
def _receipts_on(monkeypatch) -> None:
    monkeypatch.setattr(settings, "narrated_cue_receipts_enabled", True)


# -- F1 / F9: the replay is the incident ------------------------------------------------------


def test_f1_f9_with_the_receipts_off_the_moments_are_the_recorded_have_a_look(monkeypatch) -> None:
    monkeypatch.setattr(settings, "narrated_cue_receipts_enabled", False)

    for rid in MOMENTS:
        receipt = _judge(rid)
        assert RECORDED[rid]["verification"] == "unchecked"
        assert receipt.status == "partial" and not is_judged(REQUIREMENTS[rid], receipt), rid


# -- F2: the moments that played on their words -----------------------------------------------


def test_f2_each_moment_on_its_words_is_done_with_its_times() -> None:
    paintings, wall, city = _judge("r3"), _judge("r4"), _judge("r6")

    for receipt in (paintings, wall, city):
        assert (receipt.status, receipt.verification) == ("met", "checked"), receipt
    # "arkadaşlar" ends step seg_0 and "İspanyollar geldi" opens seg_1: the first word decides.
    assert paintings.reason == (
        "When you say arkadaşlar İspanyollar geldi, the clip you named is on screen (0.0–7.4 s)"
    )
    assert wall.reason == (
        "When you say İnkalar için, the clip you named is on screen (16.0–18.0 s)"
    )
    assert city.reason == (
        "When you say Osmanlı'ya dayanıyor, the clip you named is on screen (51.7–53.2 s)"
    )


def test_f2_the_review_says_it_in_turkish_too() -> None:
    from app.kria.reply_language import _CURRENT  # noqa: PLC0415

    token = _CURRENT.set("tr")
    try:
        wall = _judge("r4")
    finally:
        _CURRENT.reset(token)
    assert wall.reason == "İnkalar için dediğinde söylediğin klip ekranda (16.0–18.0 sn)"


# -- F3: the moment whose words cleanup removed -----------------------------------------------


def test_f3_words_missing_from_the_finished_voice_are_said_with_where_the_clip_plays() -> None:
    receipt = _judge("r5")

    assert receipt.status == "partial" and receipt.verification == "checked"
    assert is_judged(REQUIREMENTS["r5"], receipt)
    assert receipt.reason == (
        "I couldn't find Ant Dağları'ndan in the finished voiceover; "
        "the clip you named plays at 20.5–35.0 s"
    )


# -- F4: wrong content never passes -----------------------------------------------------------


def test_f4_the_wall_label_on_another_step_is_couldnt_with_both_times() -> None:
    steps = [dict(step) for step in CASE["narrated_alignment"]["steps"]]
    wall = next(s for s in steps if s["step_id"] == "seg_2")
    other = next(s for s in steps if s["step_id"] == "seg_3")
    wall["labels"], other["labels"] = other["labels"], wall["labels"]
    wall.pop("moment", None)

    receipt = _judge("r4", _facts(steps))

    assert receipt.status == "not_possible" and is_judged(REQUIREMENTS["r4"], receipt)
    assert receipt.reason == (
        "When you say İnkalar için, another clip is on screen (16.0–18.0 s); "
        "the clip you named plays at 18.0–20.5 s"
    )


def test_f4_a_clip_no_step_carries_keeps_the_old_answer() -> None:
    ask = REQUIREMENTS["r4"].model_copy(
        update={"description": 'Show the red llama when I say "İnkalar için".'}
    )

    receipt = _judge("r4", req=ask)

    assert not is_judged(ask, receipt), receipt


# -- F5 / F6: rows that name no shot ----------------------------------------------------------


def test_f5_fill_the_rest_keeps_its_answer() -> None:
    receipt = _judge("r7")
    assert "When you say" not in (receipt.reason or "")
    assert "couldn't find" not in (receipt.reason or "")


def test_f6_a_word_shared_with_a_generic_label_claims_nothing() -> None:
    # "the other clips" (seg_3 and others) shares "clips" with this ask, never the phrase.
    ask = REQUIREMENTS["r4"].model_copy(
        update={"description": 'Show my clips when I say "İnkalar için".'}
    )

    receipt = _judge("r4", req=ask)

    assert not is_judged(ask, receipt), receipt


# -- F7: drafts are unchanged -----------------------------------------------------------------


def test_f7_a_draft_with_no_steps_is_not_judged_here() -> None:
    facts = dataclasses.replace(_facts(), narrated_cue_steps=None)

    for rid in MOMENTS:
        assert not is_judged(REQUIREMENTS[rid], _judge(rid, facts)), rid


# -- F8: Turkish folding ----------------------------------------------------------------------


def test_f8_a_shared_first_word_elsewhere_is_not_the_phrase() -> None:
    # seg_0 says "Osmanlı'ya bağlanıyor" over the paintings; the phrase is said only in seg_10.
    steps = [dict(step) for step in CASE["narrated_alignment"]["steps"]]
    assert "Osmanlı'ya" in steps[0]["text"]

    receipt = _judge("r6")

    assert receipt.status == "met" and "(51.7–53.2 s)" in receipt.reason
