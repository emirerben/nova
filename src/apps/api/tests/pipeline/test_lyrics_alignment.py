"""Lyrics-alignment unit tests — pure function, no network."""

from __future__ import annotations

from app.pipeline.lyrics_alignment import align, align_with_line_anchors
from app.services.lrclib_client import SyncedLine
from app.services.whisper_lyrics import WhisperWord


def _ww(text: str, start: float, end: float) -> WhisperWord:
    return WhisperWord(text=text, start_s=start, end_s=end)


def test_align_simple_perfect_match() -> None:
    canonical = ["Hello world", "Foo bar"]
    whisper = [
        _ww("hello", 0.0, 0.4),
        _ww("world", 0.4, 0.9),
        _ww("foo", 1.2, 1.5),
        _ww("bar", 1.5, 1.8),
    ]
    result = align(canonical, whisper)
    assert len(result.lines) == 2
    assert result.lines[0].text == "Hello world"
    assert result.lines[0].words[0].text == "Hello"  # canonical casing preserved
    assert result.lines[0].words[0].start_s == 0.0
    assert result.lines[0].words[1].text == "world"
    assert result.lines[1].text == "Foo bar"
    assert result.confidence == 1.0


def test_align_recovers_from_mishear() -> None:
    """Whisper transposes 'world' to 'wurld' — fuzzy match still aligns it."""
    canonical = ["Hello world today"]
    whisper = [
        _ww("hello", 0.0, 0.3),
        _ww("wurld", 0.3, 0.6),
        _ww("today", 0.6, 1.0),
    ]
    result = align(canonical, whisper)
    assert len(result.lines) == 1
    assert [w.text for w in result.lines[0].words] == ["Hello", "world", "today"]
    # Timing comes from Whisper
    assert result.lines[0].words[1].start_s == 0.3
    assert result.confidence > 0.9


def test_align_interpolates_missing_word() -> None:
    """Whisper drops 'lovely' — alignment fills in interpolated timing."""
    canonical = ["Hello lovely world"]
    whisper = [_ww("hello", 0.0, 0.3), _ww("world", 1.0, 1.5)]
    result = align(canonical, whisper)
    assert len(result.lines) == 1
    words = result.lines[0].words
    assert [w.text for w in words] == ["Hello", "lovely", "world"]
    # Middle word's timing should land between the two anchors
    assert words[0].end_s <= words[1].start_s
    assert words[1].end_s <= words[2].start_s
    assert words[2].start_s == 1.0


def test_align_drops_line_with_zero_anchors() -> None:
    """A canonical line where NO word matches Whisper is dropped from output."""
    canonical = ["Apple banana cherry", "Hello world"]
    whisper = [_ww("hello", 0.0, 0.3), _ww("world", 0.3, 0.7)]
    result = align(canonical, whisper)
    assert len(result.lines) == 1
    assert result.lines[0].text == "Hello world"


def test_align_handles_diacritics_for_matching() -> None:
    """Turkish 'ü' should match 'u' in Whisper output."""
    canonical = ["Üzgünüm bebek"]
    whisper = [_ww("uzgunum", 0.0, 0.5), _ww("bebek", 0.5, 1.0)]
    result = align(canonical, whisper)
    assert len(result.lines) == 1
    # Canonical text preserves the diacritics
    assert result.lines[0].words[0].text == "Üzgünüm"


def test_align_empty_inputs_return_empty_result() -> None:
    assert align([], []).lines == ()
    assert align(["hello"], []).lines == ()
    assert align([], [_ww("hello", 0.0, 0.3)]).lines == ()


def test_align_skips_section_markers_only_when_caller_strips_them() -> None:
    """Alignment itself takes canonical lines AS GIVEN. The Genius parser
    already strips section markers; this confirms alignment doesn't add
    its own filter that might surprise callers."""
    # If [Verse] survived parsing, alignment treats it as a real line.
    canonical = ["[Verse]", "Hello"]
    whisper = [_ww("hello", 0.0, 0.3)]
    result = align(canonical, whisper)
    # The bracketed token has no match → that line drops; "Hello" remains.
    assert len(result.lines) == 1
    assert result.lines[0].text == "Hello"


# ── align_with_line_anchors ───────────────────────────────────────────────────


def test_anchored_exact_word_count_uses_fast_zip_path() -> None:
    """When Whisper produced exactly the right number of words inside an
    anchor window, every canonical word gets a REAL Whisper timing — no
    interpolation. This is the common case and the strongest quality lever
    LRCLIB synced lyrics gives us."""
    anchors = [
        SyncedLine(start_s=0.0, text="Hello world"),
        SyncedLine(start_s=2.0, text="Foo bar"),
    ]
    whisper = [
        _ww("hello", 0.1, 0.5),
        _ww("world", 0.5, 0.9),
        _ww("foo", 2.1, 2.4),
        _ww("bar", 2.4, 2.7),
    ]
    result = align_with_line_anchors(anchors, whisper, track_end_s=3.5)
    assert len(result.lines) == 2

    line0 = result.lines[0]
    assert line0.text == "Hello world"
    # Exact whisper timings preserved (within rounding to 3dp).
    assert line0.words[0].start_s == 0.1
    assert line0.words[1].end_s == 0.9
    # Canonical text wins over whisper text.
    assert line0.words[0].text == "Hello"

    # Confidence = 100% — every canonical word matched a whisper word.
    assert result.confidence == 1.0


def test_anchored_count_mismatch_falls_back_to_fuzzy_align() -> None:
    """Whisper drops or hallucinates a word inside a window. We can't zip
    directly — fall back to the fuzzy matcher constrained to the window
    so unmatched canonical words get interpolated timings."""
    anchors = [SyncedLine(start_s=0.0, text="Hello world how are you")]
    # Whisper only caught 3 of the 5 expected words.
    whisper = [
        _ww("hello", 0.0, 0.3),
        _ww("how", 0.7, 0.9),
        _ww("you", 1.2, 1.4),
    ]
    result = align_with_line_anchors(anchors, whisper, track_end_s=2.0)
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.text == "Hello world how are you"
    # All 5 canonical words present — gaps got interpolated.
    assert len(line.words) == 5
    # Confidence reflects matched-only count: 3 of 5.
    assert 0.5 < result.confidence < 0.7


def test_anchored_zero_whisper_words_in_window_interpolates_linearly() -> None:
    """A window with zero matching Whisper words (Whisper missed a quiet
    line, or there's instrumental in this section). Distribute canonical
    words uniformly across `[window_start, window_end)` so the karaoke
    line still plays at a readable pace."""
    anchors = [
        SyncedLine(start_s=0.0, text="ignored first"),
        SyncedLine(start_s=2.0, text="silent line here"),
        SyncedLine(start_s=5.0, text="later line"),
    ]
    # No whisper words in [2.0, 5.0) — Whisper missed the entire window.
    whisper = [
        _ww("ignored", 0.0, 0.5),
        _ww("first", 0.5, 1.0),
        _ww("later", 5.1, 5.4),
        _ww("line", 5.4, 5.7),
    ]
    result = align_with_line_anchors(anchors, whisper, track_end_s=6.0)
    silent_line = next(line for line in result.lines if line.text == "silent line here")
    assert len(silent_line.words) == 3
    # Linear distribution across [2.0, 5.0): slice = 1.0s each.
    assert silent_line.words[0].start_s == 2.0
    assert silent_line.words[2].end_s == 5.0


def test_anchored_last_line_uses_track_end_s_for_window() -> None:
    """The final anchor has no next-anchor to bound it. `track_end_s`
    serves as the hard right bound; without it we'd have to invent one
    and risk a karaoke line running forever."""
    anchors = [SyncedLine(start_s=10.0, text="last line")]
    whisper = [_ww("last", 10.0, 10.3), _ww("line", 10.3, 10.6)]
    result = align_with_line_anchors(anchors, whisper, track_end_s=12.0)
    assert len(result.lines) == 1
    line = result.lines[0]
    # Words used real Whisper timings (exact-count fast path).
    assert line.end_s == 10.6


def test_anchored_last_line_falls_back_to_whisper_tail_when_no_track_end() -> None:
    """When `track_end_s` is None, the helper falls back to
    `whisper_words[-1].end_s + 0.5` so the final line still has a usable
    upper bound."""
    anchors = [SyncedLine(start_s=10.0, text="orphan line")]
    whisper = [_ww("orphan", 10.0, 10.3), _ww("line", 10.3, 10.6)]
    # Don't pass track_end_s.
    result = align_with_line_anchors(anchors, whisper)
    assert len(result.lines) == 1


def test_anchored_empty_anchor_lines_returns_empty_result() -> None:
    assert align_with_line_anchors([], [_ww("x", 0.0, 0.1)]).lines == ()


def test_anchored_multi_timestamp_chorus_anchors_get_distinct_windows() -> None:
    """LRCLIB chorus expansion (one text, 3 timestamps) yields 3 anchor
    lines with the same text but different start_s. Each must get its
    own window and its own per-word timings."""
    anchors = [
        SyncedLine(start_s=0.0, text="Chorus text"),
        SyncedLine(start_s=10.0, text="Chorus text"),
        SyncedLine(start_s=20.0, text="Chorus text"),
    ]
    whisper = [
        _ww("chorus", 0.0, 0.4),
        _ww("text", 0.4, 0.9),
        _ww("chorus", 10.0, 10.4),
        _ww("text", 10.4, 10.9),
        _ww("chorus", 20.0, 20.4),
        _ww("text", 20.4, 20.9),
    ]
    result = align_with_line_anchors(anchors, whisper, track_end_s=22.0)
    assert len(result.lines) == 3
    starts = [line.start_s for line in result.lines]
    assert starts == [0.0, 10.0, 20.0]
    # Each chorus instance gets DIFFERENT word timings (no bleed).
    assert result.lines[0].words[0].start_s == 0.0
    assert result.lines[1].words[0].start_s == 10.0
    assert result.lines[2].words[0].start_s == 20.0
