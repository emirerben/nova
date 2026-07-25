"""Tests for synthesized word-reveal timings (generative-edit intro overlays).

The correctness contract: strictly-increasing cumulative ends (no two words highlight
at once), a >= MIN_WORD_CS floor per word, and beat-snapping that never reorders or
collapses words.
"""

from __future__ import annotations

from app.pipeline.word_timing import (
    MIN_WORD_CS,
    rebuild_word_timings_for_text,
    synthesize_word_timings,
)


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


# ── rebuild_word_timings_for_text (karaoke text-edit repair) ────────────────────


def _stored(words: list[str], starts_ends: list[tuple[float, float]]) -> list[dict]:
    return [
        {
            "text": w,
            "start_s": s,
            "end_s": e,
            "duration_cs": max(MIN_WORD_CS, int(round((e - s) * 100))),
        }
        for w, (s, e) in zip(words, starts_ends)
    ]


def test_rebuild_returns_stored_timings_verbatim_when_tokens_match():
    stored = _stored(["hello", "world"], [(0.0, 0.7), (0.7, 1.4)])
    out = rebuild_word_timings_for_text("hello world", stored)
    assert out is stored  # identity: beat-snapped fidelity is preserved untouched


def test_rebuild_token_match_is_case_sensitive():
    stored = _stored(["Hello", "world"], [(0.0, 0.7), (0.7, 1.4)])
    out = rebuild_word_timings_for_text("hello world", stored)
    assert out is not stored
    assert [w["text"] for w in out] == ["hello", "world"]


def test_rebuild_resynthesizes_new_words_over_the_original_window():
    # Prod job 96771038: five stale words swept while the text said "Man City".
    stored = _stored(
        ["city", "nights", "and", "friday", "football"],
        [(0.0, 0.6), (0.6, 1.2), (1.2, 1.8), (1.8, 2.4), (2.4, 3.0)],
    )
    out = rebuild_word_timings_for_text("Man City", stored)
    assert [w["text"] for w in out] == ["Man", "City"]
    # Window preserved: first start → last end.
    assert out[0]["start_s"] == 0.0
    assert out[-1]["end_s"] == 3.0
    # Even split, contiguous, schema-complete.
    assert out[0]["end_s"] == out[1]["start_s"]
    assert all({"text", "start_s", "end_s", "duration_cs"} <= set(w) for w in out)
    assert all(w["duration_cs"] >= MIN_WORD_CS for w in out)


def test_rebuild_preserves_nonzero_window_origin():
    stored = _stored(["a", "b"], [(0.5, 1.0), (1.0, 1.5)])
    out = rebuild_word_timings_for_text("x y z", stored)
    assert [w["text"] for w in out] == ["x", "y", "z"]
    assert out[0]["start_s"] == 0.5
    assert out[-1]["end_s"] == 1.5
    # Contiguous within the shifted window.
    assert out[0]["end_s"] == out[1]["start_s"]
    assert out[1]["end_s"] == out[2]["start_s"]


def test_rebuild_returns_none_without_stored_timings_or_tokens():
    assert rebuild_word_timings_for_text("hello", None) is None
    assert rebuild_word_timings_for_text("hello", []) is None
    stored = _stored(["a"], [(0.0, 1.0)])
    assert rebuild_word_timings_for_text("   ", stored) is None


def test_rebuild_returns_none_on_degenerate_window():
    stored = _stored(["a", "b"], [(2.0, 2.0), (2.0, 2.0)])
    assert rebuild_word_timings_for_text("new words", stored) is None


def test_rebuild_never_mutates_inputs():
    stored = _stored(["old", "words"], [(0.0, 1.0), (1.0, 2.0)])
    snapshot = [dict(w) for w in stored]
    rebuild_word_timings_for_text("brand new text", stored)
    assert stored == snapshot
