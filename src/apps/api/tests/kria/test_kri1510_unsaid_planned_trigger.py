"""KRI-1510: a planned sticker whose word the take never says is reported on the draft.

Cusco kit T3 "Sticker 1" (thread b6bf01c5, job a4f7b702, 2026-11-16, phone Talking): "When I
say "lama", show the llama sticker" is the kit's trap; the speaker never says it. The Main
Creator planned the llama beat anyway (its rationale: "The supplied transcript does not contain
“lama,” so its sticker will appear only if that word is actually heard"), and the draft said
"Done: Show the llama sticker when I say "lama"". Only the finished video said "I never heard
lama in your voice", one render later. KRI-1486 already reports words the take never says
("You don't say “...” in your take, so nothing is shown for it", approvable), but only when no
beat was planned for them: a planned beat counted as placed.

Ways this can go wrong (written before the code):
 P1 the draft still says "met" for a planned beat whose word no take says (today's bug);
 P2 the Turkish turn keeps the English sentence;
 P3 a planned beat whose word IS said (case, punctuation, a plural) is called unsaid, or the
    other stickers of the same draft change;
 P4 no transcript, a take with an unknown transcript, a draft off the phone Talking writer, or
    either switch off changes anything;
 P5 the draft is held instead of approvable (readiness and the receipt disagree);
 P6 the finished video's answer changes (its beat receipt, not the draft transcript, decides);
 P7 one row naming two words, one said and placed, one planned but unsaid, either stays "met"
    or loses the said word's placement;
 P8 a title logo beat (nothing listened for) is called unsaid;
 P9 a said word with no pop-in beside an unsaid planned one is hidden behind the sentence.

Real data: `tests/fixtures/kri1511_cusco_t3/sticker1.json` (the draft's transcript, the brief
rows, the planner's beats and the finished video's beat receipt). No model calls.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import app.services.phone_reaction_grounding as rg
from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
    unsaid_cue_reason,
)
from app.kria.reply_language import reply_language_for
from tests.kria.test_brief_reaction_beats import _beat, _manifest, _strategy, _take

T3 = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri1511_cusco_t3" / "sticker1.json").read_text(
        encoding="utf-8"
    )
)
# The kit's four stickers, on Visuals the shared test manifest holds.
_VISUAL = {
    "snake": "asset-pasta.png",
    "puma": "asset-pizza.png",
    "condor": "asset-labomba.png",
    "llama": "asset-paella.png",
}
T3_BEATS = [_beat(b["beat_id"], b["trigger"], _VISUAL[b["beat_id"]]) for b in T3["beats"]]
T3_REQS = [
    BriefRequirement(scope="global", **row)
    for row in T3["requirements"]
    if row["id"] in ("r1", "r2", "r3", "r4")
]
UNSAID_LAMA = "You don't say “lama” in your take, so nothing is shown for it"
UNSAID_LAMA_TR = "Çekiminde “lama” demiyorsun, o yüzden bunun için bir şey gösterilmiyor"


def _draft(*transcripts, beats=T3_BEATS, phone_talking=True):  # noqa: ANN001, ANN002, ANN202
    return dataclasses.replace(
        plan_facts_from_strategy(_strategy(beats), manifest=_manifest()),
        phone_talking=phone_talking,
        media_snapshot=_take(*transcripts) if transcripts else None,
    )


def _receipts(facts, reqs=T3_REQS) -> dict:  # noqa: ANN001
    return {r.requirement_id: r for r in build_receipts(list(reqs), facts, include_unchecked=True)}


def _req(rid: str, *triggers: str, description: str | None = None) -> BriefRequirement:
    return BriefRequirement(
        id=rid,
        kind="style",
        scope="global",
        description=description
        or "Show the stickers when I say " + " and ".join(f'"{t}"' for t in triggers) + ".",
        facts={"triggers": list(triggers), "display_mode": "pip"},
    )


# --- P1 / P3: the T3 draft ------------------------------------------------------------------


def test_p1_p3_t3_the_llama_row_says_she_never_says_lama_and_the_rest_stay_done():
    facts = _draft(T3["transcript"])
    assert len(facts.reaction_beats) == 4  # the planner's llama beat survives the repair
    receipts = _receipts(facts)
    assert (receipts["r4"].status, receipts["r4"].verification, receipts["r4"].reason) == (
        "partial",
        "checked",
        UNSAID_LAMA,
    )
    assert unsaid_cue_reason(T3_REQS[3], facts) == UNSAID_LAMA
    for rid in ("r1", "r2", "r3"):  # "yılanla", "puma." and "kondor," are said
        assert receipts[rid].status == "met", (rid, receipts[rid].reason)
        assert unsaid_cue_reason(T3_REQS[int(rid[1]) - 1], facts) is None


def test_p2_the_turkish_turn_says_it_in_turkish():
    with reply_language_for("tr"):
        assert _receipts(_draft(T3["transcript"]))["r4"].reason == UNSAID_LAMA_TR


@pytest.mark.parametrize(
    ("said", "heard"),
    [
        ("Lama, çok güzel.", True),
        ("LAMA!", True),
        ("and two lamas", True),
        ("Ve lamalar.", False),
        ("Alamanya'ya gittik.", False),
    ],
    ids=["capital-comma", "shouted", "english-plural", "turkish-plural", "inside-a-word"],
)
def test_p3_the_draft_hears_exactly_what_the_render_hears(said, heard):
    """The draft asks the render's own matcher: a word it hears keeps the planned sticker
    ("met"); one it does not (a Turkish suffix, letters inside another word) is reported, as the
    finished video would ("I never heard lama")."""
    words = [{"text": t, "start_s": float(i), "end_s": i + 0.5} for i, t in enumerate(said.split())]
    assert rg.trigger_is_heard(words, "lama") is heard
    receipt = _receipts(_draft(f"{T3['transcript']} {said}"))["r4"]
    if heard:
        assert receipt.status == "met", receipt.reason
    else:
        assert receipt.reason == UNSAID_LAMA


# --- P4: unknowns and switches ---------------------------------------------------------------


def test_p4_no_transcript_or_another_writer_keeps_todays_done():
    assert _receipts(_draft())["r4"].status == "met"
    assert _receipts(_draft(None))["r4"].status == "met"
    assert _receipts(_draft(T3["transcript"], phone_talking=False))["r4"].status == "met"


@pytest.mark.parametrize(
    "takes",
    [(T3["transcript"], None), (T3["transcript"], "Burada bir lama var.")],
    ids=["second-take-unknown", "second-take-says-it"],
)
def test_p4_two_takes_keep_their_own_answer(monkeypatch, takes):
    """Two talking takes have their own answer today (KRI-1486 U6); an unknown take is never
    "unsaid", and a take that says the word keeps the beat."""
    on = _receipts(_draft(*takes))
    monkeypatch.setattr(settings, "unsaid_planned_cue_report_enabled", False)
    assert on == _receipts(_draft(*takes))
    assert UNSAID_LAMA not in (on["r4"].reason or "")


@pytest.mark.parametrize(
    "switch", ["unsaid_planned_cue_report_enabled", "unsaid_cue_report_enabled"]
)
def test_p4_either_switch_off_is_exactly_today(monkeypatch, switch):
    on = _receipts(_draft(T3["transcript"]))
    monkeypatch.setattr(settings, switch, False)
    off = _receipts(_draft(T3["transcript"]))
    assert off["r4"].status == "met" and off["r4"].reason is None
    assert {rid: off[rid] for rid in ("r1", "r2", "r3")} == {
        rid: on[rid] for rid in ("r1", "r2", "r3")
    }


# --- P6: the finished video -----------------------------------------------------------------


def _t3_variant() -> dict:
    return {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "edit_format": "subtitled",
        "render_destination": "device",
        "phone_beat_receipt": {
            "version": 1,
            "matcher": "phrase",
            "placed": T3["placed"],
            "unplaced": T3["unplaced"],
            "closing": {"status": "none", "badge": "none"},
        },
        "overlay_transcript": T3["words"],
    }


def test_p6_the_finished_video_keeps_its_own_answer(monkeypatch):
    on = _receipts(plan_facts_from_phone_variant(_t3_variant()))
    monkeypatch.setattr(settings, "unsaid_planned_cue_report_enabled", False)
    off = _receipts(plan_facts_from_phone_variant(_t3_variant()))
    assert on == off
    assert on["r4"].status == "not_possible"
    assert "never heard lama" in on["r4"].reason
    assert all(on[rid].status == "met" for rid in ("r1", "r2", "r3"))


# --- P7 / P9: rows naming more than one word -------------------------------------------------


def test_p7_a_row_with_a_said_and_an_unsaid_planned_word_reports_only_the_unsaid_one():
    req = _req("r9", "puma", "lama")
    receipt = _receipts(_draft(T3["transcript"]), [req])["r9"]
    assert (receipt.status, receipt.reason) == ("partial", UNSAID_LAMA)
    assert unsaid_cue_reason(req, _draft(T3["transcript"])) == UNSAID_LAMA


def test_p7_two_unsaid_planned_words_are_named_together():
    beats = [*T3_BEATS, _beat("alpaca", "alpaka", "asset-badge.png")]
    req = _req("r9", "lama", "alpaka")
    receipt = _receipts(_draft(T3["transcript"], beats=beats), [req])["r9"]
    assert (
        receipt.reason == "You don't say “lama”, “alpaka” in your take, so nothing is shown for it"
    )


def test_p9_a_said_word_with_no_pop_in_is_still_said_beside_the_sentence():
    beats = [b for b in T3_BEATS if b["beat_id"] != "condor"]  # "kondor" is said, not planned
    req = _req("r9", "kondor", "lama")
    receipt = _receipts(_draft(T3["transcript"], beats=beats), [req])["r9"]
    assert UNSAID_LAMA in receipt.reason and "No pop-in for kondor" in receipt.reason
    assert receipt.reason != UNSAID_LAMA  # not the approvable sentence alone: it still holds


# --- P8: a title logo is never listened for -------------------------------------------------


def test_p8_a_title_logo_beat_is_never_called_unsaid():
    logo = {**_beat("logo", "llama logo", "asset-paella.png"), "anchor": "title"}
    req = _req("r9", "llama logo", description="Put the llama logo on the title.")
    facts = _draft(T3["transcript"], beats=[*T3_BEATS[:3], logo])
    assert unsaid_cue_reason(req, facts) is None
    assert UNSAID_LAMA not in (_receipts(facts, [req])["r9"].reason or "")
