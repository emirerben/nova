"""Lyrics-alignment unit tests — pure function, no network."""

from __future__ import annotations

import pytest

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


def test_anchored_exact_word_count_repairs_isolated_canonical_mismatch() -> None:
    """Exact-count windows still need word-agreement checks.

    Regression for lyrics-preview job 8c5793b6: the LRCLIB row had "Myself"
    where the audio/Whisper word was "body". Count-only zipping stamped the
    wrong canonical word onto the audible timing and reported full confidence.
    When the surrounding phrase agrees, trust the isolated Whisper disagreement
    for display instead of burning a word the listener never hears.
    """
    anchors = [SyncedLine(start_s=0.0, text="Myself moving heart is open")]
    whisper = [
        _ww("body", 0.1, 0.3),
        _ww("moving", 0.3, 0.6),
        _ww("heart", 0.6, 0.9),
        _ww("is", 0.9, 1.0),
        _ww("open", 1.0, 1.3),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=2.0)

    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.text == "Body moving heart is open"
    assert [w.text for w in line.words] == ["Body", "moving", "heart", "is", "open"]
    assert line.words[0].start_s == pytest.approx(0.1, abs=1e-3)
    assert result.confidence == pytest.approx(0.8, abs=1e-6)


def test_anchored_exact_word_count_rejects_multiple_canonical_mismatches() -> None:
    """Do not let Whisper author multiple displayed words in a canonical line."""
    anchors = [SyncedLine(start_s=0.0, text="Myself dancing heart is open")]
    whisper = [
        _ww("body", 0.1, 0.3),
        _ww("moving", 0.3, 0.6),
        _ww("heart", 0.6, 0.9),
        _ww("is", 0.9, 1.0),
        _ww("open", 1.0, 1.3),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=2.0)

    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.text == "Myself dancing heart is open"
    assert [w.text for w in line.words[:2]] == ["Myself", "dancing"]
    assert result.confidence == pytest.approx(0.6, abs=1e-6)


def test_anchored_window_repairs_missing_leading_whisper_prefix() -> None:
    """A short audible prefix can be missing from the lyric source.

    Regression shape from lyrics-preview job 8c5793b6: one repeated chorus
    line displayed "moving heart is open" even though Whisper/audio contained
    "body moving heart is open". If the canonical suffix fully agrees with
    Whisper, keep the short leading Whisper prefix for display.
    """
    anchors = [SyncedLine(start_s=0.0, text="moving heart is open")]
    whisper = [
        _ww("body", 0.0, 0.25),
        _ww("moving", 0.25, 0.55),
        _ww("heart", 0.55, 0.8),
        _ww("is", 0.8, 0.95),
        _ww("open", 0.95, 1.25),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=2.0)

    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.text == "Body moving heart is open"
    assert [w.text for w in line.words] == ["Body", "moving", "heart", "is", "open"]
    assert line.words[0].start_s == pytest.approx(0.0, abs=1e-3)
    assert result.confidence == pytest.approx(1.0, abs=1e-6)


def test_anchored_window_rejects_weak_one_word_suffix_prefix_repair() -> None:
    """Do not author a prefix when only one canonical suffix token matches."""
    anchors = [SyncedLine(start_s=0.0, text="open")]
    whisper = [
        _ww("is", 0.0, 0.15),
        _ww("open", 0.15, 0.45),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=1.0)

    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.text == "open"
    assert [w.text for w in line.words] == ["open"]


def test_anchored_window_rejects_distant_leading_whisper_prefix() -> None:
    """A stray word far before the suffix is not an omitted lyric prefix."""
    anchors = [SyncedLine(start_s=0.0, text="moving heart is open")]
    whisper = [
        _ww("body", 0.0, 0.25),
        _ww("moving", 1.3, 1.6),
        _ww("heart", 1.6, 1.85),
        _ww("is", 1.85, 2.0),
        _ww("open", 2.0, 2.3),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=3.0)

    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.text == "moving heart is open"
    assert [w.text for w in line.words] == ["moving", "heart", "is", "open"]
    assert line.words[0].start_s == pytest.approx(1.3, abs=1e-3)


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


def test_anchored_collapsed_word_cluster_spreads_local_phrase() -> None:
    """Prod regression: Again / Roger Sanchez job 213243c6.

    Whisper returned distinct words for the lyric text, but seven middle words
    shared the same 50ms timing. That made karaoke sit on "I" and then flash
    "don't even know why I put up" all at once.
    """
    line_text = "I swear to God, I don't even know why I put up with you"
    anchors = [
        SyncedLine(start_s=232.44, text=line_text),
        SyncedLine(start_s=235.63, text="Ok stop"),
    ]
    whisper = [
        _ww("I", 232.44, 232.86),
        _ww("swear", 232.88, 233.267),
        _ww("to", 233.307, 233.693),
        _ww("God", 233.733, 234.12),
        _ww("I", 234.14, 234.9),
        _ww("don't", 234.92, 234.97),
        _ww("even", 234.92, 234.97),
        _ww("know", 234.92, 234.97),
        _ww("why", 234.92, 234.97),
        _ww("I", 234.92, 234.97),
        _ww("put", 234.92, 234.97),
        _ww("up", 234.92, 234.97),
        _ww("with", 234.9, 235.66),
        _ww("you", 235.68, 235.93),
        _ww("Ok", 235.63, 235.86),
        _ww("stop", 235.88, 236.18),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=237.0)
    line = next(line for line in result.lines if line.text == line_text)
    words = line.words

    assert [w.text for w in words[4:13]] == [
        "I",
        "don't",
        "even",
        "know",
        "why",
        "I",
        "put",
        "up",
        "with",
    ]
    starts = [w.start_s for w in words[4:13]]
    assert len(set(starts)) == len(starts)
    for prev, nxt in zip(words[4:12], words[5:13], strict=False):
        assert prev.end_s <= nxt.start_s
        assert nxt.start_s - prev.start_s >= 0.09
    assert words[5].start_s < 234.3
    assert words[7].start_s < 234.5
    assert words[12].start_s == 234.9


def test_anchored_late_anchor_uses_matching_prefix_from_lookback_without_global_shift() -> None:
    """Prod regression: Again / Roger Sanchez job 213243c6.

    LRCLIB's line anchor for "I swear to God..." landed ~1.16s after the
    actual vocal. The old hard-window code excluded "I swear to God" before
    alignment, so the whole line rendered late. The local prefix lookback
    admits the real line start, but must not treat that one repaired line as
    whole-track drift and shift the following "Ok stop" anchor earlier.
    """
    line_text = "I swear to God, I don't even know why I put up with you"
    anchors = [
        SyncedLine(start_s=232.44, text=line_text),
        SyncedLine(start_s=235.63, text="Ok stop"),
    ]
    whisper = [
        _ww("I", 231.28, 231.38),
        _ww("swear", 231.38, 231.88),
        _ww("to", 231.88, 232.08),
        _ww("God", 232.08, 232.32),
        _ww("I", 232.80, 232.85),
        _ww("don't", 232.85, 233.12),
        _ww("even", 233.12, 233.35),
        _ww("know", 233.35, 233.57),
        _ww("why", 233.57, 233.69),
        _ww("I", 233.76, 233.79),
        _ww("put", 233.90, 234.18),
        _ww("up", 234.18, 234.40),
        _ww("with", 234.80, 235.36),
        _ww("you", 235.36, 235.58),
        _ww("Ok", 235.70, 235.98),
        _ww("stop", 236.05, 236.35),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=237.0)
    line = next(line for line in result.lines if line.text == line_text)
    ok_line = next(line for line in result.lines if line.text == "Ok stop")

    assert line.start_s == 231.28
    assert [w.start_s for w in line.words[:4]] == [231.28, 231.38, 231.88, 232.08]
    # Without excluding local prefix repairs from global re-anchor, this
    # would be rewritten to ~234.47s by the first-line -1.16s shift.
    assert ok_line.start_s == 235.7


def test_anchored_repeated_chorus_prefers_pre_anchor_prefix_over_late_decoy() -> None:
    """Regression for lyrics-preview job f6637708.

    The LRCLIB anchor for "But I love it..." landed ~2.1s after the actual
    first "But I love it" phrase. The normal anchor window still contained a
    later repeated "I love it" tail, so the old short lookback missed the real
    prefix and aligned the caption to the decoy, making both chorus lines late.
    """
    anchors = [
        SyncedLine(start_s=44.99, text="I can't feel my face when I'm with you"),
        SyncedLine(start_s=49.94, text="But I love it, but I love it, oh"),
        SyncedLine(start_s=53.06, text="I can't feel my face when I'm with you"),
        SyncedLine(start_s=59.16, text="But I love it, but I love it, oh"),
        SyncedLine(start_s=61.68, text="And I know she'll be the death of me"),
    ]
    whisper = [
        _ww("I", 44.00, 44.62),
        _ww("can", 44.62, 44.86),
        _ww("feel", 44.86, 45.16),
        _ww("my", 45.16, 45.40),
        _ww("face", 45.40, 45.72),
        _ww("when", 45.72, 45.98),
        _ww("I'm", 45.98, 46.44),
        _ww("with", 46.44, 46.54),
        _ww("you", 46.54, 46.90),
        _ww("But", 47.82, 48.44),
        _ww("I", 48.44, 48.64),
        _ww("love", 48.64, 48.94),
        _ww("it", 48.94, 49.38),
        _ww("why", 49.56, 50.70),
        _ww("I", 50.70, 50.90),
        _ww("love", 50.90, 51.24),
        _ww("it", 51.24, 51.66),
        _ww("I", 51.66, 52.60),
        _ww("can", 52.60, 53.74),
        _ww("feel", 53.74, 54.02),
        _ww("my", 54.02, 54.26),
        _ww("face", 54.26, 54.62),
        _ww("when", 54.62, 54.86),
        _ww("I'm", 54.86, 55.32),
        _ww("with", 55.32, 55.42),
        _ww("you", 55.42, 55.82),
        _ww("But", 56.74, 57.36),
        _ww("I", 57.36, 57.56),
        _ww("love", 57.56, 57.98),
        _ww("it", 57.98, 58.28),
        _ww("why", 58.58, 59.58),
        _ww("I", 59.58, 59.88),
        _ww("love", 59.88, 60.14),
        _ww("it", 60.14, 60.50),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=65.0)
    but_lines = [line for line in result.lines if line.text == "But I love it, but I love it, oh"]

    assert [line.start_s for line in but_lines] == [47.82, 56.74]
    assert [w.start_s for w in but_lines[0].words[:4]] == [47.82, 48.44, 48.64, 48.94]
    assert [w.start_s for w in but_lines[1].words[:4]] == [56.74, 57.36, 57.56, 57.98]


def test_anchored_repeated_chorus_lookback_wins_over_in_window_decoy_prefix() -> None:
    """A later repeated prefix inside the anchor window must not block lookback.

    The Can Feel My Face chorus has two "But I love it" phrases in one LRCLIB
    line. When the stale LRCLIB anchor lands after the first phrase, Whisper
    can still contain a strong prefix match for the second phrase inside the
    normal anchor window. That decoy used to skip the pre-anchor lookback and
    make the whole line render late in Pop-up and Karaoke previews.
    """
    anchors = [
        SyncedLine(start_s=44.99, text="I can't feel my face when I'm with you"),
        SyncedLine(start_s=49.94, text="But I love it, but I love it, oh"),
        SyncedLine(start_s=53.06, text="I can't feel my face when I'm with you"),
    ]
    whisper = [
        _ww("I", 44.00, 44.62),
        _ww("can", 44.62, 44.86),
        _ww("feel", 44.86, 45.16),
        _ww("my", 45.16, 45.40),
        _ww("face", 45.40, 45.72),
        _ww("when", 45.72, 45.98),
        _ww("I'm", 45.98, 46.44),
        _ww("with", 46.44, 46.54),
        _ww("you", 46.54, 46.90),
        _ww("But", 47.82, 48.44),
        _ww("I", 48.44, 48.64),
        _ww("love", 48.64, 48.94),
        _ww("it", 48.94, 49.38),
        _ww("But", 50.02, 50.70),
        _ww("I", 50.70, 50.90),
        _ww("love", 50.90, 51.24),
        _ww("it", 51.24, 51.66),
        _ww("oh", 51.66, 52.60),
        _ww("I", 52.60, 53.74),
        _ww("can", 53.74, 54.02),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=55.0)
    but_line = next(
        line for line in result.lines if line.text == "But I love it, but I love it, oh"
    )

    assert but_line.start_s == 47.82
    assert [w.start_s for w in but_line.words[:4]] == [47.82, 48.44, 48.64, 48.94]


def test_rejected_prefix_lookback_does_not_mark_line_locally_adjusted(monkeypatch) -> None:
    """A rejected lookback candidate must not exclude the line from global re-anchor."""
    from app.pipeline import lyrics_alignment

    captured: dict[str, list[bool]] = {}

    def capture_reanchor(**kwargs):
        captured["local_anchor_adjusted"] = list(kwargs["local_anchor_adjusted"])
        return kwargs["aligned_lines"]

    monkeypatch.setattr(lyrics_alignment, "_maybe_reanchor_to_lrc", capture_reanchor)

    anchors = [
        SyncedLine(start_s=10.0, text="Plain previous"),
        SyncedLine(start_s=14.0, text="But I love it, oh"),
        SyncedLine(start_s=18.0, text="Next line"),
    ]
    whisper = [
        _ww("Plain", 10.10, 10.35),
        _ww("previous", 10.35, 10.80),
        _ww("But", 12.90, 13.10),
        _ww("I", 13.10, 13.25),
        _ww("love", 13.25, 13.55),
        _ww("But", 14.10, 14.30),
        _ww("I", 14.30, 14.45),
        _ww("love", 14.45, 14.70),
        _ww("it", 14.70, 14.95),
        _ww("oh", 14.95, 15.30),
        _ww("Next", 18.10, 18.35),
        _ww("line", 18.35, 18.60),
    ]

    align_with_line_anchors(anchors, whisper, track_end_s=19.0)

    assert captured["local_anchor_adjusted"] == [False, False, False]


def test_anchored_prefix_lookback_does_not_reuse_previous_line_words() -> None:
    """The wider repeated-chorus lookback must not steal an already-rendered line."""
    anchors = [
        SyncedLine(start_s=10.0, text="But I love it, oh"),
        SyncedLine(start_s=13.6, text="But I love it, oh"),
        SyncedLine(start_s=16.0, text="Next line"),
    ]
    whisper = [
        _ww("But", 10.7, 10.9),
        _ww("I", 10.9, 11.0),
        _ww("love", 11.0, 11.2),
        _ww("it", 11.2, 11.4),
        _ww("oh", 11.4, 11.7),
        _ww("Next", 16.0, 16.2),
        _ww("line", 16.2, 16.5),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=17.0)

    but_lines = [line for line in result.lines if line.text == "But I love it, oh"]
    assert [line.start_s for line in but_lines] == [10.7, 13.6]
    assert [w.start_s for w in but_lines[1].words] == [13.6, 14.08, 14.56, 15.04, 15.52]


def test_anchored_prefix_lookback_requires_strong_prefix(monkeypatch) -> None:
    """Do not pull a line earlier from a single common pre-anchor word."""
    from app.pipeline import lyrics_alignment

    rec = _LogRecorder()
    monkeypatch.setattr(lyrics_alignment, "log", rec)

    anchors = [
        SyncedLine(start_s=10.0, text="I swear to God"),
        SyncedLine(start_s=14.0, text="next line"),
    ]
    whisper = [
        _ww("I", 9.30, 9.40),
        _ww("swear", 10.20, 10.55),
        _ww("to", 10.60, 10.75),
        _ww("God", 10.80, 11.10),
        _ww("next", 14.10, 14.30),
        _ww("line", 14.30, 14.60),
    ]

    align_with_line_anchors(anchors, whisper, track_end_s=15.0)

    assert not rec.events_named("lyrics_alignment_anchor_prefix_lookback_applied")


def test_anchored_prefix_lookback_preserves_canonical_leading_mishear() -> None:
    """Use a misheard leading token for timing without caching that text."""
    anchors = [
        SyncedLine(start_s=55.86, text="You told me stories"),
        SyncedLine(start_s=59.62, text="next line"),
    ]
    whisper = [
        _ww("We", 55.14, 55.58),
        _ww("told", 55.58, 55.84),
        _ww("me", 55.84, 56.1),
        _ww("stories", 56.1, 56.38),
        _ww("next", 59.7, 59.9),
        _ww("line", 59.9, 60.1),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=61.0)

    line = result.lines[0]
    assert line.text == "You told me stories"
    assert line.start_s == 55.14
    assert [word.text for word in line.words] == ["You", "told", "me", "stories"]


def test_anchored_prefix_lookback_rejects_distant_leading_mishear(monkeypatch) -> None:
    """A distant unrelated word must not pull a line before the anchor."""
    from app.pipeline import lyrics_alignment

    rec = _LogRecorder()
    monkeypatch.setattr(lyrics_alignment, "log", rec)

    anchors = [
        SyncedLine(start_s=55.86, text="You told me stories"),
        SyncedLine(start_s=59.62, text="next line"),
    ]
    whisper = [
        _ww("We", 54.5, 54.7),
        _ww("told", 55.58, 55.84),
        _ww("me", 55.84, 56.1),
        _ww("stories", 56.1, 56.38),
        _ww("next", 59.7, 59.9),
        _ww("line", 59.9, 60.1),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=61.0)

    line = result.lines[0]
    assert line.start_s > 55.0
    assert not rec.events_named("lyrics_alignment_anchor_prefix_lookback_applied")


def test_anchored_low_confidence_line_uses_unused_whisper_tail() -> None:
    """Regression for popup preview job 9cc0cb15.

    LRCLIB had the repeated hook's prefix right but the tail wrong. Whisper
    heard the sung tail inside the same line window; the renderer should not
    keep interpolating LRCLIB's stale tail over the next lyric line.
    """
    anchors = [
        SyncedLine(start_s=55.02, text="previous line"),
        SyncedLine(start_s=57.75, text="Don't trust me, I might fall in"),
        SyncedLine(start_s=61.39, text="I never think about you at all"),
    ]
    whisper = [
        _ww("previous", 55.1, 55.4),
        _ww("line", 55.4, 55.8),
        _ww("Trust", 56.7, 58.0),
        _ww("me", 58.0, 58.6),
        _ww("I'm", 58.6, 59.16),
        _ww("not", 59.16, 60.08),
        _ww("falling", 60.08, 60.72),
        _ww("I", 60.72, 61.64),
        _ww("never", 61.64, 62.46),
        _ww("think", 62.46, 62.84),
        _ww("about", 62.84, 63.3),
        _ww("you", 63.3, 63.66),
        _ww("at", 63.66, 63.84),
        _ww("all", 63.84, 64.62),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=65.0)

    repaired = next(line for line in result.lines if line.start_s < 58.0 < line.end_s)
    assert repaired.text == "Don't trust me I'm not falling"
    assert [w.text for w in repaired.words] == [
        "Don't",
        "trust",
        "me",
        "I'm",
        "not",
        "falling",
    ]
    assert "might" not in {w.text for w in repaired.words}
    assert repaired.end_s <= 60.72


def test_anchored_trims_duplicated_next_line_prefix_from_current_line() -> None:
    """Regression for lyrics preview jobs 481a50d2 / ca2fd867.

    The current LRCLIB row already contained the next row's opening ``when``.
    Both karaoke and pop-up previews consume this shared aligned line, so the
    aligner must remove the boundary duplicate before the cache is stored.
    """
    anchors = [
        SyncedLine(start_s=10.0, text="Hope we make it outta here when"),
        SyncedLine(start_s=13.4, text="when the night starts falling"),
    ]
    whisper = [
        _ww("Hope", 10.1, 10.35),
        _ww("we", 10.35, 10.55),
        _ww("make", 10.55, 10.8),
        _ww("it", 10.8, 10.95),
        _ww("outta", 10.95, 11.2),
        _ww("here", 11.2, 11.55),
        _ww("when", 13.45, 13.7),
        _ww("the", 13.7, 13.9),
        _ww("night", 13.9, 14.2),
        _ww("starts", 14.2, 14.5),
        _ww("falling", 14.5, 14.9),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=15.5)

    line = result.lines[0]
    assert line.text == "Hope we make it outta here"
    assert [word.text for word in line.words] == ["Hope", "we", "make", "it", "outta", "here"]
    assert result.lines[1].text == "when the night starts falling"


def test_anchored_low_confidence_tail_repair_trims_next_line_prefix() -> None:
    """The Whisper-tail repair must not steal the next line's opening word."""
    anchors = [
        SyncedLine(start_s=10.0, text="Hope stale stale stale stale stale"),
        SyncedLine(start_s=13.4, text="when the night starts falling"),
    ]
    whisper = [
        _ww("Hope", 10.1, 10.35),
        _ww("we", 10.35, 10.55),
        _ww("make", 10.55, 10.8),
        _ww("it", 10.8, 10.95),
        _ww("outta", 10.95, 11.2),
        _ww("here", 11.2, 11.55),
        _ww("when", 13.05, 13.3),
        _ww("when", 13.45, 13.7),
        _ww("the", 13.7, 13.9),
        _ww("night", 13.9, 14.2),
        _ww("starts", 14.2, 14.5),
        _ww("falling", 14.5, 14.9),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=15.5)

    line = result.lines[0]
    assert line.text == "Hope we make it outta here"
    assert [word.text for word in line.words] == ["Hope", "we", "make", "it", "outta", "here"]
    assert result.lines[1].text == "when the night starts falling"


def test_anchored_low_confidence_tail_repair_requires_tail_near_next_anchor() -> None:
    anchors = [
        SyncedLine(start_s=10.0, text="alpha beta gamma delta epsilon zeta"),
        SyncedLine(start_s=14.0, text="next line"),
    ]
    whisper = [
        _ww("gamma", 10.2, 10.5),
        _ww("other", 10.6, 10.9),
        _ww("spoken", 10.9, 11.2),
        _ww("tail", 11.2, 11.5),
        _ww("words", 11.5, 11.8),
        _ww("next", 14.1, 14.3),
        _ww("line", 14.3, 14.6),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=15.0)

    line = result.lines[0]
    assert line.text == "alpha beta gamma delta epsilon zeta"
    assert [word.text for word in line.words] == [
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "zeta",
    ]


def test_anchored_low_confidence_tail_repair_requires_prefix_match() -> None:
    anchors = [
        SyncedLine(start_s=10.0, text="alpha beta gamma delta epsilon zeta"),
        SyncedLine(start_s=14.0, text="next line"),
    ]
    whisper = [
        _ww("delta", 10.2, 10.5),
        _ww("other", 12.6, 12.9),
        _ww("spoken", 12.9, 13.2),
        _ww("tail", 13.2, 13.5),
        _ww("words", 13.5, 13.8),
        _ww("next", 14.1, 14.3),
        _ww("line", 14.3, 14.6),
    ]

    result = align_with_line_anchors(anchors, whisper, track_end_s=15.0)

    line = result.lines[0]
    assert line.text == "alpha beta gamma delta epsilon zeta"
    assert [word.text for word in line.words] == [
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "zeta",
    ]


def test_anchored_zero_whisper_words_in_window_interpolates_linearly() -> None:
    """A window with zero matching Whisper words (Whisper missed a quiet
    line, or there's instrumental in this section). Distribute canonical
    words uniformly across `[window_start, window_end)` — but with the
    per-word cap so the line clears the screen rather than holding the
    last word for the rest of the gap. The window here is 3s / 3 words =
    1s natural pace, which exceeds the 0.8s cap and gets clamped."""
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
    assert silent_line.words[0].start_s == 2.0
    # Natural pace 1.0s/word > 0.8s cap → each word is 0.8s.
    # Line ends at 2.0 + 3*0.8 = 4.4s, NOT at window_end (5.0s).
    for word in silent_line.words:
        assert word.end_s - word.start_s <= 0.81  # 0.8 + rounding slack
    assert silent_line.words[2].end_s <= 4.5


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


def test_anchored_interpolation_caps_word_duration_in_long_gap() -> None:
    """The Artbat regression: when LRC blank-text lines (instrumental
    breaks) are skipped by the parser, the preceding lyric's window
    stretches across the gap. If Whisper produced no words in that window,
    naive division would highlight each word for 10+ seconds and kill the
    karaoke pacing. The cap clamps each interpolated word to
    `_MAX_INTERP_SLICE_S` (0.8s) so highlights clear the screen at a
    readable pace and the instrumental break plays out clean."""
    anchors = [
        SyncedLine(start_s=0.0, text="five word lyric line here"),
        # Next sung anchor is 60s later — simulates a long melodic break.
        SyncedLine(start_s=60.0, text="next sung line"),
    ]
    # Whisper missed the entire first window — purely instrumental.
    whisper = [_ww("next", 60.0, 60.3), _ww("sung", 60.3, 60.6), _ww("line", 60.6, 60.9)]

    result = align_with_line_anchors(anchors, whisper, track_end_s=62.0)

    gap_line = next(line for line in result.lines if "five word" in line.text)
    assert len(gap_line.words) == 5
    # Each word capped at 0.8s — would have been 12s without the fix.
    for word in gap_line.words:
        assert word.end_s - word.start_s <= 0.81  # 0.8 + rounding slack
    # Line ends 5 * 0.8 = 4.0s in, NOT at window_end (60.0).
    assert gap_line.end_s <= 4.1, (
        f"line should clear the screen at ~4s, not run to {gap_line.end_s}"
    )


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


# ─────────────────────────────────────────────────────────────────────────────
# Trailing-collapse guardrail
# ─────────────────────────────────────────────────────────────────────────────


class _LogRecorder:
    """Stand-in for the module-level structlog `log`. structlog's BoundLogger
    bypasses pytest's caplog by default, so we monkeypatch the module
    attribute and inspect calls directly. Pattern lifted from
    tests/pipeline/test_lyric_injector.py."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def info(self, event, **kwargs):
        self.events.append(("info", event, kwargs))

    def warning(self, event, **kwargs):
        self.events.append(("warning", event, kwargs))

    def debug(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def events_named(self, name: str) -> list[dict]:
        return [k for _level, e, k in self.events if e == name]


def test_trailing_unmatched_tail_spreads_distinct_slots_not_collapsed() -> None:
    """The f65b5762 / Hawai class: LRCLIB next-line anchor lands earlier
    than the actual next vocal, so the trailing canonical tokens for the
    current line never match any Whisper word inside the hard window.

    Pre-fix code collapsed all trailing tokens onto a single 250ms window
    at `prev_end + 0.02s`, baking a wrong `line.end_s` into the cache. The
    guardrail spreads them across the available tail with distinct slots.
    """
    anchors = [
        SyncedLine(start_s=2.9, text="I didn't wanna be the one to forget"),
        SyncedLine(start_s=5.0, text="I thought of everything I'd never regret"),
    ]
    whisper = [
        _ww("I", 2.95, 3.07),
        _ww("didn", 3.07, 3.57),
        _ww("'t", 3.57, 3.81),
        _ww("wanna", 3.82, 4.45),
        _ww("be", 4.45, 4.70),
        # "the" lands inside [2.9, 5.0) start-wise so it still matches.
        _ww("the", 4.70, 5.07),
        # "one to forget" start past 5.0 — excluded by the hard window.
        _ww("one", 5.07, 5.43),
        _ww("to", 5.47, 5.69),
        _ww("forget", 5.69, 6.44),
        _ww("I", 7.19, 7.29),
        _ww("thought", 7.29, 8.04),
        _ww("of", 8.04, 8.25),
        _ww("everything", 8.25, 9.33),
        _ww("I", 9.33, 9.43),
        _ww("'d", 9.43, 9.63),
        _ww("never", 9.64, 10.18),
        _ww("regret", 10.18, 10.83),
    ]
    result = align_with_line_anchors(anchors, whisper, track_end_s=12.0)

    line_a = next(line for line in result.lines if line.text.startswith("I didn"))
    # "one to forget" — three trailing unmatched canonical tokens. Pre-fix
    # behavior collapsed all three onto [prev_end + 0.02, prev_end + 0.27].
    trailing = line_a.words[-3:]
    starts = sorted(w.start_s for w in trailing)
    assert len(set(starts)) == 3, f"trailing words should have distinct starts, got {starts}"
    # Adjacent trailing words are spaced by at least 0.15s (not bunched
    # at prev_end + 0.02s, which was the pre-fix collapse pattern).
    for prev_s, next_s in zip(starts, starts[1:], strict=False):
        assert next_s - prev_s >= 0.15, (
            f"trailing words too close ({next_s - prev_s:.3f}s) — "
            "pre-fix collapse pattern resurfaced"
        )
    # Line end extends substantially past the LRCLIB next-anchor (5.0s),
    # since the canonical line's audio actually runs to ~6.4s. The pre-fix
    # collapse would have put line.end_s ~4.95s — readers saw text disappear
    # 1.5s before "forget" finished.
    assert line_a.end_s > 5.5, (
        f"line.end_s ({line_a.end_s}) should reach into the real audio tail; "
        "if it stops near the drifted LRCLIB anchor, the trailing-collapse bug returned"
    )


def test_trailing_unmatched_tail_logs_collapse_event_when_triggered(
    monkeypatch,
) -> None:
    """The guardrail emits `lyrics_alignment_trailing_collapse` once per
    occurrence so the rate of this fallback is visible in prod logs."""
    from app.pipeline import lyrics_alignment

    rec = _LogRecorder()
    monkeypatch.setattr(lyrics_alignment, "log", rec)

    anchors = [
        SyncedLine(start_s=0.0, text="match match miss miss miss miss"),
        SyncedLine(start_s=1.0, text="next"),
    ]
    whisper = [
        _ww("match", 0.0, 0.2),
        _ww("match", 0.2, 0.4),
        _ww("next", 1.0, 1.2),
    ]
    align_with_line_anchors(anchors, whisper, track_end_s=2.0)
    events = rec.events_named("lyrics_alignment_trailing_collapse")
    assert len(events) >= 1, (
        f"expected at least one trailing_collapse event, got {[e for _, e, _ in rec.events]}"
    )
    ev = events[0]
    assert ev["unmatched_count"] == 4
    assert ev["canonical_tail"] == ["miss", "miss", "miss", "miss"]


def test_trailing_unmatched_tail_uses_conservative_budget_when_no_cap() -> None:
    """The unanchored `align()` path has no per-line window. The
    guardrail falls back to a half-budget formula (still bounded) instead
    of using a runaway tail_end_cap_s = None branch.
    """
    canonical = ["match miss miss miss"]
    # Whisper matches only the first word.
    whisper = [_ww("match", 0.0, 0.5)]
    result = align(canonical, whisper)
    assert len(result.lines) == 1
    line = result.lines[0]
    # Trailing "miss miss miss" — bounded by the conservative formula:
    # _MAX_INTERP_SLICE_S=0.8 * 3 tokens * 0.5 factor = 1.2s budget total,
    # capped per-token at _MAX_INTERP_SLICE_S. Each token gets ~0.4s; no
    # token's end exceeds prev_end + budget.
    trailing = line.words[-3:]
    assert trailing[-1].end_s < 0.5 + 0.8 * 3 + 0.1  # well within the budget cap
    # Each trailing word has its own slot.
    starts = [w.start_s for w in trailing]
    assert len(set(starts)) == 3


def test_trailing_spread_clamped_to_next_line_safety_margin() -> None:
    """When `tail_end_cap_s` is supplied, the spread must leave a
    `_NEXT_LINE_SAFETY_S = 0.05` spacer before the cap so the trailing
    tokens never land at the same timestamp as the next anchor.
    """
    anchors = [
        SyncedLine(start_s=0.0, text="a b miss"),
        SyncedLine(start_s=1.0, text="z"),
    ]
    whisper = [
        _ww("a", 0.0, 0.1),
        _ww("b", 0.1, 0.2),
        # No "miss" in window.
        _ww("z", 1.0, 1.1),
    ]
    result = align_with_line_anchors(anchors, whisper, track_end_s=2.0)
    line_a = next(line for line in result.lines if line.text == "a b miss")
    # "miss" must end at most window_end - _NEXT_LINE_SAFETY_S = 0.95
    assert line_a.words[-1].end_s <= 0.95 + 1e-6


def test_trailing_spread_single_unmatched_does_not_log_collapse(monkeypatch) -> None:
    """A single trailing unmatched token is not the collapse pattern —
    no `lyrics_alignment_trailing_collapse` log fires for tail_count < 2.
    The guardrail target is 2+ collapsed trailing tokens.
    """
    from app.pipeline import lyrics_alignment

    rec = _LogRecorder()
    monkeypatch.setattr(lyrics_alignment, "log", rec)

    anchors = [
        SyncedLine(start_s=0.0, text="a b miss"),
        SyncedLine(start_s=2.0, text="z"),
    ]
    whisper = [
        _ww("a", 0.0, 0.3),
        _ww("b", 0.3, 0.6),
        _ww("z", 2.0, 2.3),
    ]
    align_with_line_anchors(anchors, whisper, track_end_s=3.0)
    assert not rec.events_named("lyrics_alignment_trailing_collapse"), (
        "should NOT log collapse for a single trailing unmatched token"
    )


def test_single_trailing_unmatched_with_cap_respects_safety_margin() -> None:
    """Pins the scope-expanded tail_count == 1 behavior in the anchored path.

    The pre-fix code gave a single trailing unmatched word a fixed 0.25s
    window. The bounded spread formula now gives it the cap-derived duration
    (up to `_MAX_INTERP_SLICE_S = 0.8s`), but MUST still respect
    `tail_end_cap_s - _NEXT_LINE_SAFETY_S` so it can never extend into the
    next line. Scope-expansion is intentional (single missing word should
    reflect a real karaoke duration) — this test pins it.
    """
    anchors = [
        SyncedLine(start_s=0.0, text="a b missing"),
        # Next anchor at 3.0s — plenty of cap room for the single missing word.
        SyncedLine(start_s=3.0, text="next"),
    ]
    whisper = [
        _ww("a", 0.0, 0.3),
        _ww("b", 0.3, 0.6),
        # No Whisper word for "missing" inside [0, 3) — single trailing
        # unmatched token.
        _ww("next", 3.0, 3.2),
    ]
    result = align_with_line_anchors(anchors, whisper, track_end_s=4.0)
    line_a = next(line for line in result.lines if line.text == "a b missing")
    missing = line_a.words[-1]
    # Pre-fix 0.25s would put missing.end_s at ~0.87s. The new bounded
    # spread gives the canonical _MAX_INTERP_SLICE_S = 0.8s slice
    # (rounded), so the duration is around 0.78-0.80s — substantially
    # more than 0.25s but still well below the cap.
    duration = missing.end_s - missing.start_s
    assert duration > 0.3, (
        f"single trailing unmatched should get >0.3s, got {duration:.3f}s "
        "— pre-fix 0.25s collapse pattern returned"
    )
    assert duration <= 0.81, (
        f"single trailing unmatched should be capped at _MAX_INTERP_SLICE_S "
        f"(~0.8s with rounding), got {duration:.3f}s"
    )
    # Safety margin: missing.end_s must NOT reach the next anchor.
    next_anchor_start = 3.0
    assert missing.end_s <= next_anchor_start - 0.04, (
        f"single trailing unmatched ends at {missing.end_s}, must respect "
        f"_NEXT_LINE_SAFETY_S = 0.05 before next anchor at {next_anchor_start}"
    )


def test_single_trailing_unmatched_cap_with_no_room_falls_back_to_conservative() -> None:
    """When the supplied `tail_end_cap_s` leaves no usable room past
    `prev_end` (the previously matched Whisper word already ran into the
    next line's anchor window), the single-trailing branch uses the
    conservative no-cap budget formula. End must still NOT cross the next
    line's first matched Whisper word, because the global next-line
    protection is enforced by Pass 2 / per-line index bounds (future PR);
    in the meantime the conservative per-token cap is the only bound.
    """
    anchors = [
        SyncedLine(start_s=0.0, text="a b missing"),
        # Next anchor at only 0.65s — earlier than the actual vocal end.
        SyncedLine(start_s=0.65, text="next"),
    ]
    whisper = [
        _ww("a", 0.0, 0.3),
        # "b" extends past anchor B (0.65s) — its end at 0.7s overran the
        # hard window. cap_budget becomes negative; conservative fires.
        _ww("b", 0.3, 0.7),
        _ww("next", 0.65, 0.85),
    ]
    result = align_with_line_anchors(anchors, whisper, track_end_s=2.0)
    line_a = next(line for line in result.lines if line.text == "a b missing")
    missing = line_a.words[-1]
    duration = missing.end_s - missing.start_s
    # Conservative formula: _MAX_INTERP_SLICE_S * tail_count * 0.5 = 0.4s.
    # Per-token cap clamps at _MAX_INTERP_SLICE_S = 0.8s. So duration is
    # 0.38s (0.4 - _INTERPOLATION_PADDING_S).
    assert 0.35 <= duration <= 0.45, (
        f"conservative budget should produce ~0.4s slice, got {duration:.3f}s"
    )


def test_single_trailing_unmatched_no_cap_logs_for_visibility(monkeypatch) -> None:
    """The unanchored `align()` path passes no `tail_end_cap_s` — when a
    single trailing unmatched token uses the conservative budget, emit the
    `lyrics_alignment_trailing_single_no_cap` event so the rate of this
    low-confidence fallback is visible in prod. Documented substitute for
    a per-line low_conf marker until the data model gains one.
    """
    from app.pipeline import lyrics_alignment

    rec = _LogRecorder()
    monkeypatch.setattr(lyrics_alignment, "log", rec)

    # Single line, single trailing miss.
    canonical = ["match miss"]
    whisper = [_ww("match", 0.0, 0.5)]
    align(canonical, whisper)

    events = rec.events_named("lyrics_alignment_trailing_single_no_cap")
    assert len(events) == 1, (
        f"expected exactly one no-cap-single event, got {[e for _, e, _ in rec.events]}"
    )
    assert events[0]["canonical_tail"] == ["miss"]
    # The collapse log MUST NOT also fire (tail_count == 1).
    assert not rec.events_named("lyrics_alignment_trailing_collapse")
