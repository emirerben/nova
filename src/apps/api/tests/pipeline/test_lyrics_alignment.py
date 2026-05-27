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
