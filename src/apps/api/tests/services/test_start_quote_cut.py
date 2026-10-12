"""KRI-628 guard: "start the video at '…'" trims the cut plan to open on the quoted line.

Real T1 words (Tigre cabin kit, 2026-10-12): the intro "Evet, reis. Hazırlıklara başladın
mı? Hadi, çıkacağız az sonra. Evet." precedes the question the creator wants first.
Failure modes: reading the intro's own "başladın" as the start verb; cutting when the
line is not said / already first / deep into the take; leaving removals inside the
dropped intro; dropping the quoted line's first word.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.pipeline.silence_cut import CutPlan, Removal, no_op_plan
from app.services.start_quote_cut import apply_start_quote_cut, start_quote_request

WORDS = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri632_t1_talking_words.json").read_text()
)
DURATION = WORDS[-1]["end_s"] + 1.0
BRIEF = (
    "Baştaki “Hazırlıklara başladın mı” kısmını ve uzun duraksamaları kes; video "
    "“Abonelerimize bir şey demek ister misin” sorusuyla başlasın."
)
QUOTE = "Abonelerimize bir şey demek ister misin"


def _intro_end() -> float:
    return max(w["end_s"] for w in WORDS if w["end_s"] <= _question_start())


def _question_start() -> float:
    return next(w["start_s"] for w in WORDS if w["text"].startswith("Abonelerimize"))


def test_the_start_quote_is_the_one_next_to_the_start_verb() -> None:
    assert start_quote_request([BRIEF]) == QUOTE
    assert start_quote_request(['Start the video at "let us talk about Lisbon"']) == (
        "let us talk about Lisbon"
    )


def test_quotes_without_a_start_verb_are_not_start_cuts() -> None:
    assert (
        start_quote_request(["Karaoke altyazı; “community” ve “cringe” İngilizce yazılsın."])
        is None
    )
    assert start_quote_request(['Cut the "um we are starting now" intro']) is None
    assert start_quote_request(['End on "thanks for watching everyone"']) is None


def test_the_plan_opens_on_the_quoted_line_and_keeps_its_first_word() -> None:
    plan, receipt = apply_start_quote_cut(no_op_plan(DURATION), WORDS, QUOTE, duration_s=DURATION)
    assert receipt["status"] == "applied"
    ((start, end),) = plan.keep_segments
    assert end == DURATION
    assert _intro_end() < start <= _question_start()
    assert plan.removed[0].reason == "start_quote" and plan.removed[0].start_s == 0.0
    assert "Hazırlıklara" in receipt["removed_text"]
    assert abs(plan.time_saved_s - start) < 1e-6


def test_pause_cuts_inside_the_dropped_intro_are_absorbed_and_later_ones_kept() -> None:
    q = _question_start()
    plan = CutPlan(
        keep_segments=[(0.0, 2.0), (3.0, 12.0), (14.0, DURATION)],
        removed=[Removal(2.0, 3.0, "long_pause"), Removal(12.0, 14.0, "long_pause")],
        time_saved_s=3.0,
    )
    out, receipt = apply_start_quote_cut(plan, WORDS, QUOTE, duration_s=DURATION)
    assert receipt["status"] == "applied" and q > 3.0
    assert _intro_end() < out.keep_segments[0][0] <= q
    assert [r.start_s for r in out.removed] == [0.0, 12.0]
    covered = sum(e - s for s, e in out.keep_segments) + sum(
        r.end_s - r.start_s for r in out.removed
    )
    assert abs(covered - DURATION) < 1e-3  # keep + removed still partition the take


def test_an_unsafe_ask_leaves_the_plan_alone_and_says_why() -> None:
    base = no_op_plan(DURATION)
    for quote, status in (
        ("a line nobody ever says here", "not_found"),
        ("Evet reis Hazırlıklara başladın mı", "too_early"),
        ("Hadi iyi akşamlar", "too_late"),
    ):
        out, receipt = apply_start_quote_cut(base, WORDS, quote, duration_s=DURATION)
        assert out is base and receipt["status"] == status, (quote, receipt)
