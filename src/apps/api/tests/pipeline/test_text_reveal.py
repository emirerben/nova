"""Unit tests for app.pipeline.text_reveal.build_cumulative_stages."""

from __future__ import annotations

import pytest

from app.pipeline.text_reveal import (
    LAST_WORD_DWELL_S,
    MIN_RENDERABLE_S,
    CumulativeStage,
    Word,
    build_cumulative_stages,
)


def _w(text: str, start: float, end: float) -> Word:
    return Word(text=text, start_s=start, end_s=end)


def test_empty_input_returns_empty():
    assert build_cumulative_stages([], line_end_s=5.0) == []


def test_three_words_butted_edges():
    words = [_w("good", 0.0, 1.0), _w("morning", 1.0, 2.0), _w("everyone", 2.0, 3.0)]
    stages = build_cumulative_stages(words, line_end_s=3.0)
    assert len(stages) == 3

    assert stages[0].text == "good"
    assert stages[1].text == "good morning"
    assert stages[2].text == "good morning everyone"

    assert stages[0].pop_animated_suffix == "good"
    assert stages[1].pop_animated_suffix == "morning"
    assert stages[2].pop_animated_suffix == "everyone"

    # Butted: stage[i].end_s == stage[i+1].start_s for non-terminal stages.
    assert stages[0].end_s == pytest.approx(stages[1].start_s)
    assert stages[1].end_s == pytest.approx(stages[2].start_s)

    # Terminal stage extends past line_end_s by dwell.
    assert stages[2].end_s == pytest.approx(3.0 + LAST_WORD_DWELL_S)


def test_single_word_no_expansion():
    stages = build_cumulative_stages([_w("hello", 0.0, 1.0)], line_end_s=1.0)
    assert len(stages) == 1
    assert stages[0].text == "hello"
    assert stages[0].pop_animated_suffix == "hello"
    assert stages[0].start_s == pytest.approx(0.0)
    assert stages[0].end_s == pytest.approx(1.0 + LAST_WORD_DWELL_S)


def test_last_word_dwell_overrides_default():
    stages = build_cumulative_stages(
        [_w("hi", 0.0, 0.5)], line_end_s=0.5, dwell_s=1.0
    )
    assert stages[0].end_s == pytest.approx(1.5)


def test_middle_stage_dropped_when_below_min_renderable():
    # word B has a natural span of (C.start - B.start) = 0.01s < MIN_RENDERABLE_S.
    words = [
        _w("good", 0.0, 0.5),
        _w("very", 1.0, 1.005),
        _w("morning", 1.01, 2.0),
    ]
    stages = build_cumulative_stages(words, line_end_s=2.0)
    # B should be DROPPED; its text still appears in C's cumulative.
    assert len(stages) == 2
    assert stages[0].text == "good"
    assert stages[1].text == "good very morning"
    assert stages[1].pop_animated_suffix == "morning"
    # A's end_s butts directly to C's start_s (skips dropped B).
    assert stages[0].end_s == pytest.approx(stages[1].start_s)


def test_last_word_always_survives_even_when_short():
    # last word has natural span = line_end_s - last.start_s = 0.01 < MIN_RENDERABLE_S.
    words = [_w("hello", 0.0, 1.0), _w("there", 2.0, 2.0)]
    stages = build_cumulative_stages(words, line_end_s=2.01)
    # Both kept; the terminal stage's end_s is extended by dwell which pads
    # the renderable window even though natural span was below threshold.
    assert len(stages) == 2
    assert stages[1].text == "hello there"
    assert stages[1].end_s == pytest.approx(2.01 + LAST_WORD_DWELL_S)


def test_whitespace_only_word_filtered_from_cumulative():
    # Empty-after-strip words contribute nothing to the cumulative join; caller
    # is responsible for filtering, but the helper is defensive.
    words = [_w("hello", 0.0, 1.0), _w("   ", 1.0, 2.0), _w("world", 2.0, 3.0)]
    stages = build_cumulative_stages(words, line_end_s=3.0)
    # The whitespace stage emits as a stage (it survives the renderable
    # check) but its cumulative text omits the blank word, so its text equals
    # the prior stage's text. Caller should have filtered.
    assert stages[0].text == "hello"
    assert "world" in stages[-1].text


def test_terminal_stage_with_start_past_line_end_is_padded():
    # Pathological input: word starts after line_end_s. Helper still emits a
    # stage with at least MIN_RENDERABLE_S window so caller can validate.
    stages = build_cumulative_stages(
        [_w("late", 5.0, 6.0)], line_end_s=4.0, dwell_s=0.0
    )
    assert len(stages) == 1
    assert stages[0].end_s - stages[0].start_s == pytest.approx(MIN_RENDERABLE_S)


def test_all_middle_words_dropped_keeps_first_and_last():
    # Tightly-clustered short middle words. Each KEEP decision is based on
    # natural span to NEXT word's start_s, not raw duration. So:
    #   a: span to b = 1.0s → KEEP
    #   b: span to c = 0.01s → DROP
    #   c: span to d = 0.01s → DROP
    #   d: span to e = 0.98s → KEEP (because e starts long after d)
    #   e: terminal → KEEP
    words = [
        _w("a", 0.0, 0.1),
        _w("b", 1.0, 1.005),
        _w("c", 1.01, 1.015),
        _w("d", 1.02, 1.025),
        _w("e", 2.0, 2.5),
    ]
    stages = build_cumulative_stages(words, line_end_s=2.5)
    assert len(stages) == 3
    assert [s.pop_animated_suffix for s in stages] == ["a", "d", "e"]
    assert stages[0].text == "a"
    assert stages[1].text == "a b c d"  # cumulative carries DROPPED b, c
    assert stages[2].text == "a b c d e"
    # Butted edges across the dropped middle: a.end == d.start, d.end == e.start.
    assert stages[0].end_s == pytest.approx(stages[1].start_s)
    assert stages[1].end_s == pytest.approx(stages[2].start_s)


def test_returns_cumulative_stage_dataclass():
    stages = build_cumulative_stages([_w("x", 0.0, 1.0)], line_end_s=1.0)
    assert isinstance(stages[0], CumulativeStage)
