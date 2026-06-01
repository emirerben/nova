"""Tests for synthesized word-reveal timings (generative-edit intro overlays).

The correctness contract: strictly-increasing cumulative ends (no two words highlight
at once), a >= MIN_WORD_CS floor per word, and beat-snapping that never reorders or
collapses words.
"""

from __future__ import annotations

from app.pipeline.word_timing import MIN_WORD_CS, synthesize_word_timings


def _cumulative_ends(timings: list[dict]) -> list[float]:
    acc = 0.0
    out = []
    for w in timings:
        acc += w["duration_cs"] / 100.0
        out.append(acc)
    return out


def test_even_split_n_words_n_entries():
    timings = synthesize_word_timings(["one", "two", "three", "four"], 0.0, 4.0)
    assert [w["text"] for w in timings] == ["one", "two", "three", "four"]
    assert len(timings) == 4
    # 4s / 4 words → ~1s each.
    assert all(abs(w["duration_cs"] - 100) <= 1 for w in timings)


def test_cumulative_ends_strictly_increasing_even_split():
    timings = synthesize_word_timings(["a", "b", "c", "d", "e"], 0.0, 3.0)
    ends = _cumulative_ends(timings)
    assert all(ends[i] > ends[i - 1] for i in range(1, len(ends)))


def test_min_duration_floor_when_window_tiny():
    # 6 words in 0.1s → even split is well below the floor; every word must still
    # clear MIN_WORD_CS and stay strictly increasing.
    timings = synthesize_word_timings(["a", "b", "c", "d", "e", "f"], 0.0, 0.1)
    assert all(w["duration_cs"] >= MIN_WORD_CS for w in timings)
    ends = _cumulative_ends(timings)
    assert all(ends[i] > ends[i - 1] for i in range(1, len(ends)))


def test_single_word():
    timings = synthesize_word_timings(["solo"], 0.0, 2.0)
    assert len(timings) == 1
    assert timings[0]["text"] == "solo"
    assert timings[0]["duration_cs"] == 200


def test_zero_words_returns_empty():
    assert synthesize_word_timings([], 0.0, 2.0) == []
    assert synthesize_word_timings(["", "   "], 0.0, 2.0) == []


def test_non_positive_window_returns_empty():
    assert synthesize_word_timings(["a", "b"], 2.0, 2.0) == []
    assert synthesize_word_timings(["a", "b"], 3.0, 1.0) == []


def test_whitespace_tokens_dropped():
    timings = synthesize_word_timings(["hi", "  ", "there"], 0.0, 2.0)
    assert [w["text"] for w in timings] == ["hi", "there"]


def test_beat_snap_within_window_only():
    # Beats at 0.9 and 1.9 (in-window) plus 5.0 (out of window, must be ignored).
    timings = synthesize_word_timings(["a", "b", "c"], 0.0, 3.0, beats=[0.9, 1.9, 5.0])
    ends = _cumulative_ends(timings)
    # First two ends pulled toward the in-window beats; nothing snapped to 5.0.
    assert all(e <= 3.0 + 1e-6 for e in ends[:-1])
    assert all(ends[i] > ends[i - 1] for i in range(1, len(ends)))


def test_beat_snap_never_collapses_two_words_onto_same_beat():
    # Single in-window beat near both even-split targets. Only one word may claim
    # it; the other keeps its even-split slot. Ends must stay strictly increasing.
    timings = synthesize_word_timings(["a", "b", "c", "d"], 0.0, 2.0, beats=[1.0])
    ends = _cumulative_ends(timings)
    assert all(ends[i] > ends[i - 1] for i in range(1, len(ends)))
    assert len(set(round(e, 3) for e in ends)) == len(ends)


def test_beat_snap_never_moves_backwards():
    # A late beat must not pull an early word's end back before its predecessor.
    timings = synthesize_word_timings(["a", "b", "c"], 0.0, 6.0, beats=[5.5])
    ends = _cumulative_ends(timings)
    assert all(ends[i] > ends[i - 1] for i in range(1, len(ends)))


def test_string_coercion_of_non_str_tokens():
    timings = synthesize_word_timings([1, 2, 3], 0.0, 3.0)
    assert [w["text"] for w in timings] == ["1", "2", "3"]
