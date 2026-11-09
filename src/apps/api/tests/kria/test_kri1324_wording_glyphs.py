"""KRI-1286 recurrence + KRI-1324: no wording question for her own quoted ending, and no
candidate a phone font cannot draw.

Qendresa Q2 run 2 (prod thread 9db44dc2, 2026-11-08): she typed `End with "comment REPO" and an
arrow.` The Main Creator wrote ``closing_title = "comment REPO ➔"``. The KRI-1286 strip only knew
a fixed list of arrows (no "➔", U+2794), so Kria asked "I'd try 'comment REPO ➔'. Does this
wording work?" for words she gave exactly. She tapped "Use this wording" and the phone render
failed: "No phone font can draw '➔'" (job 4620fd7c, ``phone_plan_unsupported``).

Ways this can fail, written before the fix:
 F1 the incident: an arrow glyph outside the old list keeps her quoted ending in the question.
 F2 the incident's second half: Kria offers (and, once approved, keeps) a candidate holding a
    character no curated phone font draws, so the approved wording can never render.
 F3 over-reach: an undrawable LETTER (another script) is silently cut out of a candidate,
    changing the words instead of letting the draft name it (KRI-1024).
 F4 over-reach: a colour emoji (drawn by the phone's own emoji face) is stripped.
 F5 over-reach: a glyph she typed inside her own quotes is changed (her words are hers).
 F6 over-reach: a different wording plus a glyph skips the question.
"""

from __future__ import annotations

import pytest

from tests.kria.test_creative_copy_planner import _asks_wording, _closing_turn

# Event 004 of the incident, word for word.
Q2_PROMPT = (
    "Start with me full screen and the title \"5 PROJECTS / the AI builder community / can't "
    'stop talking about!!!!" in bold caps, then serif, then handwritten, with the GitHub logo '
    "above it. After that, put my screen recordings full screen and me cut out (no background) "
    "small in the bottom right corner while I talk. Zoom into the recordings so the text is "
    "readable, and show the right one when I talk about each repo. Show the trending page when "
    'I talk about what\'s hot this week. Add a label when each one starts: "1. Spec-kit", '
    '"2. Impeccable", "3. TradingAgents", "4. Agents", "5. Claude code game studio". Captions '
    "word by word in a light grey box near the bottom. Cut all my pauses. End with "
    '"comment REPO" and an arrow. Only my voice.'
)


@pytest.mark.asyncio
async def test_f1_the_incident_ending_is_hers_without_the_arrow_glyph(monkeypatch):
    result, closing = await _closing_turn(
        "comment REPO ➔", request=Q2_PROMPT, evidence=None, monkeypatch=monkeypatch
    )
    assert not _asks_wording(result)
    assert closing == "comment REPO"


@pytest.mark.parametrize("glyph", ["➔", "⮕", "⇨", "➡", "👉", "👇🏽"])
@pytest.mark.asyncio
async def test_f1_any_trailing_arrow_is_dropped_when_she_asked_for_a_drawn_arrow(
    monkeypatch, glyph
):
    result, closing = await _closing_turn(
        f"comment REPO {glyph}", request=Q2_PROMPT, evidence=None, monkeypatch=monkeypatch
    )
    assert not _asks_wording(result)
    assert closing == "comment REPO"


@pytest.mark.asyncio
async def test_f2_a_kria_candidate_never_offers_an_undrawable_glyph(monkeypatch):
    result, _ = await _closing_turn(
        "Follow for part 2 ➔",
        request="End with something that makes them follow",
        evidence=None,
        monkeypatch=monkeypatch,
    )
    assert _asks_wording(result)
    assert result.plan.choice_question["candidate"] == "Follow for part 2"
    assert "➔" not in result.plan.response


@pytest.mark.asyncio
async def test_f2_her_quote_plus_an_undrawable_glyph_is_hers_even_without_an_arrow_ask(
    monkeypatch,
):
    result, closing = await _closing_turn(
        "comment REPO ➔",
        request='End with "comment REPO" for the last 3 seconds',
        evidence=None,
        monkeypatch=monkeypatch,
    )
    assert not _asks_wording(result)
    assert closing == "comment REPO"


@pytest.mark.asyncio
async def test_f3_an_undrawable_letter_is_never_cut_out_of_the_words(monkeypatch):
    result, _ = await _closing_turn(
        "안녕 friends", request="End with a hello", evidence=None, monkeypatch=monkeypatch
    )
    assert _asks_wording(result)
    assert result.plan.choice_question["candidate"] == "안녕 friends"


@pytest.mark.asyncio
async def test_f4_a_colour_emoji_stays_in_a_candidate(monkeypatch):
    result, _ = await _closing_turn(
        "Big news 🔥", request="End with something exciting", evidence=None, monkeypatch=monkeypatch
    )
    assert _asks_wording(result)
    assert result.plan.choice_question["candidate"] == "Big news 🔥"


@pytest.mark.asyncio
async def test_f5_a_glyph_inside_her_own_quotes_is_kept(monkeypatch):
    result, closing = await _closing_turn(
        "Go ➔ now",
        request='End with "Go ➔ now" please',
        evidence=None,
        monkeypatch=monkeypatch,
    )
    assert not _asks_wording(result)
    assert closing == "Go ➔ now"


@pytest.mark.asyncio
async def test_f6_other_words_plus_a_glyph_are_still_asked(monkeypatch):
    result, _ = await _closing_turn(
        "comment CODE ➔", request=Q2_PROMPT, evidence=None, monkeypatch=monkeypatch
    )
    assert _asks_wording(result)
    assert result.plan.choice_question["candidate"] == "comment CODE"
