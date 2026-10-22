"""Voiceover rule window failures are enumerated in .dev/voiceover-text-plan.md.

Rules must retain exact copy and never overlap or acquire invented source times.
These pure timing checks supplement the worker/compiler/save journey checks.
"""

from types import SimpleNamespace

import pytest

from app.kria.spoken_text_ask import SpokenTextAsk
from app.pipeline.phone_spoken_text import spoken_text_rows

CANVAS = SimpleNamespace(width=1080, height=1920)


def resolve(asks, words, duration=10):
    return spoken_text_rows(
        asks,
        words=words,
        cues=[],
        caption_style="sentence",
        timeline_duration_s=duration,
        canvas=CANVAS,
        place=lambda row, avoid: (row, {"status": "clear"}),
        sequential=True,
    )


def word(text, start):
    return {"text": text, "start_s": start, "end_s": start + 0.2}


def test_three_exact_rules_are_sorted_and_never_cover_each_other():
    asks = [
        SpokenTextAsk("r3", "3 · 20 minutes of darkness", ("darkness",)),
        SpokenTextAsk("r1", "1 · leave before 11", ("leave",)),
        SpokenTextAsk("r2", "2 · you're all amigos", ("amigos",)),
    ]
    rows, receipt = resolve(asks, [word("leave", 1), word("amigos", 2), word("darkness", 3)])
    assert [r["text"] for r in rows] == [asks[1].text, asks[2].text, asks[0].text]
    assert [(r["start_s"], r["end_s"]) for r in rows] == [(1, 2), (2, 3), (3, 7)]
    assert all(r["status"] == "placed" for r in receipt["rows"])


def test_repeated_cue_uses_first_occurrence_and_missing_cue_has_receipt():
    rows, receipt = resolve(
        [SpokenTextAsk("r1", "Hello", ("hello",)), SpokenTextAsk("r2", "Missing", ("missing",))],
        [word("hello", 1), word("hello", 5)],
    )
    assert len(rows) == 1 and rows[0]["start_s"] == 1
    assert receipt["rows"][1]["status"] == "not_heard"


def test_same_time_distinct_rules_are_reported_as_conflicting_not_stacked():
    rows, receipt = resolve(
        [SpokenTextAsk("r1", "One", ("hi",)), SpokenTextAsk("r2", "Two", ("hi",))], [word("hi", 1)]
    )
    assert rows == []
    assert {r["status"] for r in receipt["rows"]} == {"overlap"}


def test_end_window_is_clamped_and_too_short_window_is_not_rendered():
    rows, receipt = resolve(
        [SpokenTextAsk("r1", "One", ("hi",)), SpokenTextAsk("r2", "Two", ("bye",))],
        [word("hi", 8.5), word("bye", 9.8)],
    )
    assert rows[0]["end_s"] == 9.8
    assert receipt["rows"][1]["status"] == "no_time"


@pytest.mark.parametrize("start", [None, float("nan"), -1, 11])
def test_invalid_source_time_never_becomes_zero(start):
    rows, receipt = resolve(
        [SpokenTextAsk("r", "Hi", ("hi",))], [{"text": "hi", "start_s": start, "end_s": 12}]
    )
    assert not rows
    assert receipt["rows"][0]["status"] == "error"


def test_long_exact_copy_is_reported_not_silently_truncated():
    rows, receipt = resolve([SpokenTextAsk("r", "word " * 20, ("hi",))], [word("hi", 1)])
    assert not rows
    assert receipt["rows"][0]["status"] == "text_too_long"


def test_numbered_rules_do_not_match_the_intro_count():
    asks = [
        SpokenTextAsk("r1", "1 · leave before 11", ("1", "leave before 11")),
        SpokenTextAsk("r2", "2 · you're all amigos", ("2", "you're all amigos")),
        SpokenTextAsk("r3", "3 · 20 minutes of darkness", ("3", "20 minutes of darkness")),
    ]
    words = [
        word("Three", 0),
        word("rules", 0.3),
        word("Rule", 2),
        word("one", 2.3),
        word("Rule", 4),
        word("two", 4.3),
        word("Rule", 6),
        word("three", 6.3),
    ]
    rows, _ = resolve(asks, words)
    assert [row["start_s"] for row in rows] == [2, 4, 6]


def test_ambiguous_bare_number_never_guesses_the_intro_count():
    rows, receipt = resolve(
        [SpokenTextAsk("r3", "3 · darkness", ("3",))], [word("three", 0), word("three", 4)]
    )
    assert not rows
    assert receipt["rows"][0]["status"] == "not_heard"


def test_contracted_trigger_matches_expanded_recording():
    rows, _ = resolve(
        [SpokenTextAsk("r2", "2 · you're all amigos", ("you're all amigos",))],
        [word("you", 2), word("are", 2.3), word("all", 2.6), word("amigos", 2.9)],
    )
    assert rows[0]["start_s"] == 2


def test_intro_count_alone_does_not_ground_a_numbered_rule():
    rows, receipt = resolve(
        [SpokenTextAsk("r3", "3 · darkness", ("3",))], [word("three", 0), word("rules", 0.3)]
    )
    assert not rows
    assert receipt["rows"][0]["status"] == "not_heard"
