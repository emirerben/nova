"""KRI-1285: a draft reply lists render promises as planned, not as a failure.

Q1 run 2 (2026-11-07 20:20Z): every promise receipt (pauses, five labels, the ending arrow)
read "Partly" under "Not everything you asked for made it in". Real reply writer, the
incident's own promise texts; the control keeps a genuine shortfall failing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.kria import brief_checks as bc
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.contracts import RequirementReceipt
from app.kria.reply_language import reply_language_for

_TURN = "17e9fb29-b7e4-453a-9d59-62410cefd660"


def _req(rid: str, text: str) -> BriefRequirement:
    return BriefRequirement(
        id=rid, kind="text", scope="global", description=text, source_turn_id=_TURN
    )


def _brief(*reqs: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=1, requirements=list(reqs))


def _partial(rid: str, reason: str) -> RequirementReceipt:
    return RequirementReceipt(
        requirement_id=rid, status="partial", reason=reason, verification="checked"
    )


def test_only_promises_is_not_a_failure():
    brief = _brief(_req("r1", "Show label one"), _req("r2", "Draw an arrow"))
    receipts = [
        _partial("r1", bc._TALKING_SPOKEN_TEXT_PLAN),
        _partial("r2", bc._CLOSING_ARROW_PLAN),
    ]
    text, lead = bc.reply_and_lead_from_receipts(brief, receipts, summary="All set.")
    assert "Not everything you asked for" not in text
    assert "Will do at render" in text
    assert "Partly" not in text
    assert "Partly done" not in lead


def test_real_shortfall_keeps_the_failure_heading():
    brief = _brief(_req("r1", "Show label one"), _req("r2", "Draw an arrow"))
    receipts = [
        _partial("r1", bc._TALKING_SPOKEN_TEXT_PLAN),
        _partial("r2", bc._CLOSING_ARROW_MISSING),
    ]
    text, lead = bc.reply_and_lead_from_receipts(brief, receipts, summary="All set.")
    assert text.startswith("Not everything you asked for made it in")
    assert "Will do at render: Show label one" in text
    assert "Partly: Draw an arrow" in text
    assert lead == "Partly done."


def test_compacted_reasons_end_on_a_whole_word(monkeypatch):
    # The older shortening (reasons cut evenly), kept behind KRIA_REPLY_LINE_FIT_ENABLED=false.
    monkeypatch.setattr(settings, "kria_reply_line_fit_enabled", False)
    reqs = [_req(f"r{i}", f"Label {i}") for i in range(12)]
    reason = "I'll draw a curly arrow next to your ending text " * 3
    receipts = [_partial(r.id, reason.strip()) for r in reqs]
    receipts[0] = _partial("r0", bc._CLOSING_ARROW_MISSING)
    text, _ = bc.reply_and_lead_from_receipts(_brief(*reqs), receipts)
    assert "…" in text
    assert "nex…" not in text


# --- KRI-1485: a long reply is never cut mid-sentence ------------------------------------------
# Qendresa Q2 run 3 (job 77c63496): the finished list ended "- Done: Show this label a…" at
# 1200 characters and so hid its only row that was not Done (label 5 isn't shown); the simpler
# draft and its approval text ended "...if I don't hear the word, it won't…" and hid the
# Clean-up-speech and arrow promises. Ways the fitter could fail (written before it):
# - a Partly / Couldn't / Will-do row, or its reason, is dropped or shortened;
# - rows that differ in status or reason are folded into one line, or a folded line loses a
#   label's words;
# - the "…and N more done" count is wrong, missing, or not said in Turkish / German;
# - a reply that fits is changed at all; a line is cut in the middle;
# - with the switch off the text is not exactly the old cut.

_Q2 = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri1485_q2_run3_replies.json").read_text(
        encoding="utf-8"
    )
)
_Q2_BRIEF = CreativeBrief.model_validate(_Q2["brief"])
_LABELS_2_TO_4 = '("2. Impeccable", "3. TradingAgents", "4. Agents")'


def _q2_receipts(name: str) -> list[RequirementReceipt]:
    return [RequirementReceipt.model_validate(raw) for raw in _Q2[name]]


def _whole_lines(text: str) -> None:
    """No line is cut: an ellipsis only ever opens the count line."""
    for line in text.splitlines():
        assert not line.rstrip().endswith("…"), line


def test_q2_run3_finished_list_shows_the_one_row_that_is_not_done():
    text, lead = bc.reply_and_lead_from_receipts(_Q2_BRIEF, _q2_receipts("review_receipts"))

    assert lead == "Partly done."
    # r9, the only row that is not Done, whole and with its reason.
    assert (
        "- Partly: Show this label as text on screen when I start talking about that repo, not as "
        'a clip label; replace the previous repo label. ("5. Claude code game studio") (I didn\'t '
        "hear you say “Claude code game studio” in your take, so “5. Claude code game studio” "
        "isn't shown)"
    ) in text
    # The three identical Done label rows are one line that names each label.
    assert text.count("replace the previous repo label.") == 2
    assert f"replace the previous repo label. {_LABELS_2_TO_4}" in text
    for said in ("Opening title in bold caps", '("1. Spec-kit")', "Captions word by word"):
        assert said in text
    # What still does not fit is counted, never cut: pauses, the ending, no music (all Done).
    assert text.splitlines()[-1] == "- …and 3 more done"
    _whole_lines(text)
    assert len(text) <= bc.MAX_REPLY_CHARS


def test_q2_run3_approval_text_keeps_every_promise():
    """The simpler draft's receipt lines, which the approval card repeats: every promise the
    render carries out is there, whole."""
    text = bc.reply_from_receipts(_Q2_BRIEF, _q2_receipts("draft_receipts"))

    for promise in (
        "Choose Clean up speech when you approve and every pause is cut down to about 0.3 s",
        "I'll draw a curly arrow next to your ending text",
        "I'll zoom onto the text when the video is made; I can't confirm it before then",
    ):
        assert f"({promise})" in text
    # Five identical label promises: label 1 has its own wording, labels 2-5 are one line.
    said = (
        "(I'll show it the moment you say that word in your take; if I don't hear the word, it "
        "won't appear)"
    )
    assert text.count(said) == 2
    assert (
        '("2. Impeccable", "3. TradingAgents", "4. Agents", "5. Claude code game studio")' in text
    )
    assert text.count("- Will do at render:") == 5
    _whole_lines(text)


def test_q2_run3_with_the_switch_off_the_replies_are_exactly_the_stored_ones(monkeypatch):
    monkeypatch.setattr(settings, "kria_reply_line_fit_enabled", False)
    review = bc.reply_from_receipts(_Q2_BRIEF, _q2_receipts("review_receipts"))
    assert review == _Q2["stored_review_reply"].split("\n- Left out as agreed")[0]
    draft = bc.reply_from_receipts(_Q2_BRIEF, _q2_receipts("draft_receipts"))
    stored = _Q2["stored_draft_reply"].split("\n", 1)[1].split("\n- Left out as agreed")[0]
    assert draft == stored


def _met(rid: str, reason: str | None = None) -> RequirementReceipt:
    return RequirementReceipt(
        requirement_id=rid, status="met", reason=reason, verification="checked"
    )


def _label_req(rid: str, literal: str, description: str = "Show this label") -> BriefRequirement:
    return BriefRequirement(
        id=rid,
        kind="text",
        scope="global",
        description=description,
        literal=literal,
        source_turn_id=_TURN,
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_a_reply_that_fits_is_never_changed(monkeypatch, enabled):
    monkeypatch.setattr(settings, "kria_reply_line_fit_enabled", enabled)
    reqs = [_label_req(f"r{i}", f"{i}. Label") for i in range(1, 6)]
    receipts = [_met(r.id) for r in reqs[:4]] + [_partial("r5", bc._CLOSING_ARROW_MISSING)]
    text, _ = bc.reply_and_lead_from_receipts(_brief(*reqs), receipts)
    assert text.count("\n- ") == 5  # under the cap: five rows, nothing folded
    assert '- Done: Show this label ("1. Label")' in text


def test_rows_that_are_not_done_alone_past_the_cap_get_todays_shortening(monkeypatch):
    """Twelve Partly rows alone are longer than the cap. The chat never gets more than 1200
    characters, so this one text falls back to the old shortening, byte for byte (review of
    the Q2 commits)."""
    reason = "The arrow isn't in the video yet; you can draw one with the pen in the editor"
    misses = [
        _label_req(f"p{i}", f"GO {i}", f"End clip {i} with this text and a hand-drawn arrow")
        for i in range(12)
    ]
    done = [
        _label_req(f"d{i}", f"TIP {i}", f"Show this tip on clip {i}, a little above the middle")
        for i in range(6)
    ]
    receipts = [_partial(r.id, reason) for r in misses] + [_met(r.id, "It is there") for r in done]
    brief = _brief(*misses, *done)
    text, _ = bc.reply_and_lead_from_receipts(brief, receipts)
    monkeypatch.setattr(settings, "kria_reply_line_fit_enabled", False)
    old, _ = bc.reply_and_lead_from_receipts(brief, receipts)

    assert text == old
    assert len(text) <= bc.MAX_REPLY_CHARS


def test_the_reply_never_runs_past_the_cap_when_the_lines_to_look_at_are_long(monkeypatch):
    """The "have a look" lines are not fitted: when they alone fill the chat, the reply is
    today's cut, never longer than 1200 characters."""
    done = [_label_req(f"d{i}", f"TIP {i}", f"Show this tip on clip {i}") for i in range(6)]
    miss = _label_req("p1", "GO", "End with this text and a hand-drawn arrow")
    looks = [
        _req(f"l{i}", f"Put the photo of step {i} next to me when I explain it") for i in range(9)
    ]
    receipts = [
        *(_met(r.id) for r in done),
        _partial("p1", bc._CLOSING_ARROW_MISSING),
        *(
            RequirementReceipt(
                requirement_id=r.id,
                status="partial",
                reason="It shows from 0:12 for 3 s; have a look at whether it covers the text "
                "you are pointing at in the recording",
                verification="unchecked",
            )
            for r in looks
        ),
    ]
    brief = _brief(*done, miss, *looks)
    text, _ = bc.reply_and_lead_from_receipts(brief, receipts)
    monkeypatch.setattr(settings, "kria_reply_line_fit_enabled", False)
    old, _ = bc.reply_and_lead_from_receipts(brief, receipts)

    assert "Have a look at these in the video:" in text
    assert len(text) <= bc.MAX_REPLY_CHARS
    assert text == old


def test_done_outcome_rows_lose_their_reasons_before_any_is_counted():
    """The render's own group / label rows ("Done: text for clip 3 (reason)") carry their
    reason inside the line: it goes before any Done row is counted away."""
    long_reason = "it is on screen for the whole clip, in the middle, clear of your face"
    outcomes = [
        {"status": "met", "name": f"text for clip {i} of the walk", "reason": long_reason}
        for i in range(14)
    ]
    miss = _req("p1", "End with a hand-drawn arrow")
    text, _ = bc.reply_and_lead_from_receipts(
        _brief(miss), [_partial("p1", bc._CLOSING_ARROW_MISSING)], outcomes=outcomes
    )

    assert "…and" not in text
    for i in range(14):
        assert f"- Done: text for clip {i} of the walk" in text
    assert f"- Partly: End with a hand-drawn arrow ({bc._CLOSING_ARROW_MISSING})" in text
    assert text.count(long_reason) < 14
    assert len(text) <= bc.MAX_REPLY_CHARS
    _whole_lines(text)


def test_a_composed_reply_drops_done_reasons_before_it_counts_any_row():
    """A reply fitted as text (the held reply) reads the reason off a Done line with no quoted
    text too, nested brackets included, and drops it before counting any row."""
    reason = "It is kept exactly as you filmed it, every second of it (1.6 s outro too)"
    lines = [
        "Not everything you asked for made it in:",
        *(f"- Done: Keep clip {i} as filmed ({reason})" for i in range(8)),
        "- Partly: Add a song (A Talking video can't do this, so it stays as filmed)",
    ]
    text = "\n".join(lines)
    room = len(text) - 4 * len(f" ({reason})")
    fitted = bc.fit_reply_text(text, room)

    assert len(fitted) <= room
    assert "- …and" not in fitted
    for i in range(8):
        assert f"- Done: Keep clip {i} as filmed" in fitted
    assert fitted.endswith(
        "- Partly: Add a song (A Talking video can't do this, so it stays as filmed)"
    )
    assert 0 < fitted.count(f"({reason})") < 8
    assert not any(line.endswith("(1.6 s outro too)") for line in fitted.splitlines()[-4:])


def test_done_rows_lose_their_reasons_before_any_row_is_folded_into_the_count():
    done = [
        _label_req(f"d{i}", f"TIP {i}", f"Show this tip on clip number {i}, above the middle")
        for i in range(12)
    ]
    miss = _label_req("p1", "GO", "End with this text and a hand-drawn arrow")
    long_reason = "It is " + "a good deal " * 6 + "bigger now"
    receipts = [_met(r.id, long_reason) for r in done] + [_partial("p1", bc._CLOSING_ARROW_MISSING)]
    text, _ = bc.reply_and_lead_from_receipts(_brief(*done, miss), receipts)

    # Every ask is still named; only Done reasons went, last rows first.
    for req in (*done, miss):
        assert f'{req.description} ("{req.literal}")' in text
    assert "…and" not in text
    assert (
        f'- Partly: End with this text and a hand-drawn arrow ("GO") ({bc._CLOSING_ARROW_MISSING})'
        in text
    )
    assert 0 < text.count(long_reason) < 12
    assert text.rindex(long_reason) < text.index('("TIP 11")')
    assert len(text) <= bc.MAX_REPLY_CHARS
    _whole_lines(text)


def test_only_rows_with_the_same_status_ask_and_reason_fold():
    filler = [
        _label_req(
            f"f{i}", f"NOTE {i}", "Keep this exact sentence in the video " * 4 + f"(part {i})"
        )
        for i in range(3)
    ]
    ask = "Show this label as text on screen when I start talking about it"
    labels = [_label_req(f"r{i}", f"{i}. Label", ask) for i in range(1, 6)]
    other = _label_req("r6", "6. Label", description=f"{ask}, at the very end")
    receipts = [
        *(_partial(r.id, "It is only partly there") for r in filler),
        _met("r1"),
        _met("r2"),
        _met("r3", "It is on screen for 2 s"),  # its own reason: its own line
        _partial("r4", "I didn't hear you say it"),  # not Done: its own line
        _met("r5"),
        _met("r6"),  # another ask: its own line
    ]
    brief = _brief(*filler, *labels, other)
    text, _ = bc.reply_and_lead_from_receipts(brief, receipts)

    # Folding alone made it fit: nothing is counted, no reason is dropped.
    assert f'- Done: {ask} ("1. Label", "2. Label", "5. Label")' in text
    assert f'- Done: {ask} ("3. Label") (It is on screen for 2 s)' in text
    assert f'- Partly: {ask} ("4. Label") (I didn\'t hear you say it)' in text
    assert f'- Done: {ask}, at the very end ("6. Label")' in text
    assert text.count("- Partly: Keep this exact sentence") == 3  # different asks never fold
    assert "…" not in text
    assert len(text) <= bc.MAX_REPLY_CHARS


@pytest.mark.parametrize(
    ("language", "line", "misses_n"),
    # Turkish lines are shorter: one more Partly row brings the reply over the cap the same way.
    [("en", "…and 5 more done", 7), ("tr", "…ve 5 tane daha yapıldı", 8)],
)
def test_the_count_line_counts_folded_labels_and_speaks_the_reply_language(
    language, line, misses_n
):
    # The rows that are not Done fit on their own (else the old shortening applies).
    misses = [
        _label_req(f"p{i}", f"GO {i}", f"End clip {i} with this exact text and a hand-drawn arrow")
        for i in range(misses_n)
    ]
    labels = [_label_req(f"r{i}", f"{i}. Label") for i in range(1, 5)]
    last = _label_req("d1", "THE END", "Close with this text")
    receipts = [
        *(_partial(r.id, bc._CLOSING_ARROW_MISSING) for r in misses),
        *(_met(r.id) for r in labels),
        _met("d1"),
    ]
    with reply_language_for(language):
        text, _ = bc.reply_and_lead_from_receipts(_brief(*misses, *labels, last), receipts)
    assert text.splitlines()[-1] == f"- {line}"  # four folded labels + one more ask
    assert len(text) <= bc.MAX_REPLY_CHARS


def test_the_count_line_is_german_on_a_german_turn(monkeypatch):
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True)
    first, second = "- Erledigt: " + "a" * 60, "- Erledigt: " + "b" * 40
    with reply_language_for("de"):
        assert bc.fit_reply_text(f"{first}\n{second}", 100) == f"{first}\n- …und 1 weitere erledigt"
