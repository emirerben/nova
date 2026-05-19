"""Lyrics-alignment unit tests — pure function, no network."""

from __future__ import annotations

from app.pipeline.lyrics_alignment import align
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
