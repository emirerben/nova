"""Multi-line median re-anchor regression suite.

Layered ABOVE the single-L0 `_AUDIO_SHIFT_THRESHOLD_S` check (PR #363).
The multi-line median path catches the Overnight + The Bay class:
small consistent drift (~0.3-0.7s) across every aligned line that
the single-L0 path can't safely detect (Whisper L0 noise on clean
tracks sits in the same range).

Path precedence inside `_maybe_reanchor_to_lrc`:

    1. Multi-line median  (>= 3 eligible lines, |median| > 0.2s, MAD < 0.22s)
    2. Single-L0          (|L0 shift| > 1.0s, unchanged from PR #363)
    3. No shift

Per-word `AlignedWord` timings stay byte-identical regardless of which
path fires — karaoke `\\kf` and per-word-pop consume per-word values.

Eligibility: a line counts toward the median only if its alignment had
at least 2 real Whisper word matches. Strategy 3 lines (pure linear
interpolation, matched_count = 0) would emit `shift = 0` by construction
and pull the median toward zero, silently disabling drift detection.
"""

from __future__ import annotations

import pytest

from app.pipeline.lyrics_alignment import (
    _MULTILINE_MATCHED_COUNT_THRESHOLD,
    _MULTILINE_MIN_APPLY_SHIFT_S,
    _MULTILINE_MIN_ELIGIBLE_LINES,
    _REANCHOR_NEXT_LINE_SAFETY_S,
    AlignedLine,
    align_with_line_anchors,
)
from app.services.lrclib_client import SyncedLine
from app.services.whisper_lyrics import WhisperWord


class _LogRecorder:
    """Captures structlog calls so tests can assert event names + fields.

    Mirrors the pattern used in test_lyrics_alignment.py
    `test_trailing_unmatched_tail_logs_collapse_event_when_triggered`.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.events.append(("info", event, dict(kwargs)))

    def warning(self, event: str, **kwargs: object) -> None:
        self.events.append(("warning", event, dict(kwargs)))

    def events_named(self, name: str) -> list[dict]:
        return [data for _, ev, data in self.events if ev == name]


def _consistent_aligned_track(
    *,
    line_count: int,
    shift_s: float,
    anchor_step_s: float = 5.0,
    anchor_start_s: float = 10.0,
) -> tuple[list[SyncedLine], list[WhisperWord]]:
    """Build a synthetic LRC + Whisper track with uniform shift.

    Every anchor has 2 canonical words; Whisper produces both per anchor
    shifted by `shift_s`. Each line aligns via Strategy 1 (exact-count
    zip), so `matched_count = 2` per line — every line eligible for the
    multi-line median.
    """
    anchors = []
    words = []
    for i in range(line_count):
        anchor_t = anchor_start_s + i * anchor_step_s
        word_t = anchor_t + shift_s
        anchors.append(SyncedLine(start_s=anchor_t, text=f"word{i}a word{i}b"))
        words.append(WhisperWord(text=f"word{i}a", start_s=word_t, end_s=word_t + 0.2))
        words.append(WhisperWord(text=f"word{i}b", start_s=word_t + 0.2, end_s=word_t + 0.4))
    return anchors, words


# ────────────────────────────────────────────────────────────────────────────
# Path 1: multi-line median fires
# ────────────────────────────────────────────────────────────────────────────


class TestMultiLineMedianApplies:
    """Sub-second consistent drift triggers the multi-line median path."""

    def test_consistent_subsecond_shift_applies_median(self, monkeypatch) -> None:
        """4 lines all shifted +0.4s with low spread → median (~0.4s) applied
        to every line. Catches the Overnight + The Bay class.
        """
        from app.pipeline import lyrics_alignment

        rec = _LogRecorder()
        monkeypatch.setattr(lyrics_alignment, "log", rec)

        anchors, words = _consistent_aligned_track(line_count=4, shift_s=0.4)
        result = align_with_line_anchors(anchors, words, track_end_s=60.0)

        # All lines re-anchored to anchor + 0.4
        assert len(result.lines) == 4
        assert result.lines[0].start_s == pytest.approx(10.4, abs=1e-2)
        assert result.lines[1].start_s == pytest.approx(15.4, abs=1e-2)
        assert result.lines[2].start_s == pytest.approx(20.4, abs=1e-2)
        assert result.lines[3].start_s == pytest.approx(25.4, abs=1e-2)

        # Multi-line path applied, single-L0 path never ran
        applied = rec.events_named("lyrics_alignment_reanchor_multiline_applied")
        assert len(applied) == 1
        assert applied[0]["path"] == "multi_line"
        assert applied[0]["eligible_count"] == 4
        assert applied[0]["median_shift_s"] == pytest.approx(0.4, abs=1e-2)
        assert applied[0]["spread_mad_s"] == pytest.approx(0.0, abs=1e-3)
        assert applied[0]["inlier_count"] == 4
        assert applied[0]["refined_median_s"] == pytest.approx(0.4, abs=1e-2)
        # No single-L0 event
        assert not rec.events_named("lyrics_alignment_reanchor_single_l0_applied")

    def test_per_word_timings_unchanged_after_median_shift(self) -> None:
        """Karaoke contract: AlignedWord values stay byte-identical
        pre/post re-anchor regardless of which path fired.
        """
        anchors, words = _consistent_aligned_track(line_count=4, shift_s=0.4)
        result = align_with_line_anchors(anchors, words, track_end_s=60.0)
        # L0 line bound is re-anchored to 10.4, but the first per-word value
        # is the Whisper value (10.4) — they match because Strategy 1 zip
        # uses Whisper edges directly. The invariant we're locking is that
        # the per-word value matches the source WhisperWord, not that it
        # matches the re-anchored line bound.
        l0_word = result.lines[0].words[0]
        assert l0_word.start_s == pytest.approx(10.4, abs=1e-2)
        assert l0_word.end_s == pytest.approx(10.6, abs=1e-2)


# ────────────────────────────────────────────────────────────────────────────
# Path 1: multi-line median skips
# ────────────────────────────────────────────────────────────────────────────


class TestMultiLineMedianSkips:
    """Eligibility / spread guards correctly reject false-positives."""

    def test_skipped_when_fewer_than_3_eligible_lines(self, monkeypatch) -> None:
        """Only 2 anchors → 2 eligible lines → multi-line skipped (need >=3).
        Single-L0 also skipped (shift = 0.4 < 1.0). No re-anchor.
        """
        from app.pipeline import lyrics_alignment

        rec = _LogRecorder()
        monkeypatch.setattr(lyrics_alignment, "log", rec)

        anchors, words = _consistent_aligned_track(line_count=2, shift_s=0.4)
        result = align_with_line_anchors(anchors, words, track_end_s=60.0)

        # Line bounds derive from Whisper (no re-anchor)
        assert result.lines[0].start_s == pytest.approx(10.4, abs=1e-2)
        # But line.end_s = Whisper-derived, NOT anchor[i+1] + shift
        # (since no shift applied).
        skipped = rec.events_named("lyrics_alignment_reanchor_multiline_skipped")
        assert len(skipped) == 1
        assert skipped[0]["reason"] == "insufficient_eligible_lines"
        assert skipped[0]["eligible_count"] == 2

        # Single-L0 path also skipped because 0.4 < 1.0 threshold
        no_shift = rec.events_named("lyrics_alignment_reanchor_no_shift")
        assert len(no_shift) == 1

    def test_skipped_when_median_below_threshold(self, monkeypatch) -> None:
        """4 lines with tiny consistent drift (~0.08s, below 0.2s min).
        Multi-line skipped because median too small. Single-L0 also skipped.
        """
        from app.pipeline import lyrics_alignment

        rec = _LogRecorder()
        monkeypatch.setattr(lyrics_alignment, "log", rec)

        anchors, words = _consistent_aligned_track(line_count=4, shift_s=0.08)
        align_with_line_anchors(anchors, words, track_end_s=60.0)

        skipped = rec.events_named("lyrics_alignment_reanchor_multiline_skipped")
        assert any(s["reason"] == "median_too_small" for s in skipped)

    def test_skipped_when_spread_too_wide(self, monkeypatch) -> None:
        """Eligible lines with varying shifts (high spread) → reject.
        Defends against non-uniform drift being treated as uniform.
        """
        from app.pipeline import lyrics_alignment

        rec = _LogRecorder()
        monkeypatch.setattr(lyrics_alignment, "log", rec)

        anchors = [
            SyncedLine(start_s=10.0, text="a b"),
            SyncedLine(start_s=15.0, text="c d"),
            SyncedLine(start_s=20.0, text="e f"),
            SyncedLine(start_s=25.0, text="g h"),
        ]
        # Shifts: 0.4, 0.8, 0.1, 1.2 — MAD = 0.35 > 0.22 cap
        words = [
            WhisperWord(text="a", start_s=10.4, end_s=10.6),
            WhisperWord(text="b", start_s=10.6, end_s=10.8),
            WhisperWord(text="c", start_s=15.8, end_s=16.0),
            WhisperWord(text="d", start_s=16.0, end_s=16.2),
            WhisperWord(text="e", start_s=20.1, end_s=20.3),
            WhisperWord(text="f", start_s=20.3, end_s=20.5),
            WhisperWord(text="g", start_s=26.2, end_s=26.4),
            WhisperWord(text="h", start_s=26.4, end_s=26.6),
        ]
        align_with_line_anchors(anchors, words, track_end_s=60.0)

        skipped = rec.events_named("lyrics_alignment_reanchor_multiline_skipped")
        assert any(s["reason"] == "spread_too_wide" for s in skipped)

    def test_strategy3_lines_excluded_from_median(self, monkeypatch) -> None:
        """Lines with matched_count = 0 (pure linear interpolation, Strategy 3)
        must not pull the median toward zero. Construct a track where 2 lines
        have no Whisper words in window (Strategy 3, matched=0, would emit
        shift=0) and 3 lines have real matches with shift=0.4. Eligible
        = 3 lines, median = 0.4 → applies. If Strategy 3 lines leaked into
        eligible set, median would be ~0.16 and the path would skip.
        """
        from app.pipeline import lyrics_alignment

        rec = _LogRecorder()
        monkeypatch.setattr(lyrics_alignment, "log", rec)

        anchors = [
            SyncedLine(start_s=10.0, text="alpha beta"),  # Strategy 3 (no words)
            SyncedLine(start_s=15.0, text="word0a word0b"),  # matched
            SyncedLine(start_s=20.0, text="gamma delta"),  # Strategy 3 (no words)
            SyncedLine(start_s=25.0, text="word1a word1b"),  # matched
            SyncedLine(start_s=30.0, text="word2a word2b"),  # matched
        ]
        words = [
            # Anchor 1 (15.0): shift +0.4
            WhisperWord(text="word0a", start_s=15.4, end_s=15.6),
            WhisperWord(text="word0b", start_s=15.6, end_s=15.8),
            # Anchor 3 (25.0): shift +0.4
            WhisperWord(text="word1a", start_s=25.4, end_s=25.6),
            WhisperWord(text="word1b", start_s=25.6, end_s=25.8),
            # Anchor 4 (30.0): shift +0.4
            WhisperWord(text="word2a", start_s=30.4, end_s=30.6),
            WhisperWord(text="word2b", start_s=30.6, end_s=30.8),
        ]
        align_with_line_anchors(anchors, words, track_end_s=60.0)

        applied = rec.events_named("lyrics_alignment_reanchor_multiline_applied")
        assert len(applied) == 1
        assert applied[0]["eligible_count"] == 3
        assert applied[0]["median_shift_s"] == pytest.approx(0.4, abs=1e-2)
        # matched_counts should show 0 for the Strategy 3 lines.
        assert applied[0]["matched_counts"] == [0, 2, 0, 2, 2]


# ────────────────────────────────────────────────────────────────────────────
# Path 2: single-L0 fallback (per-PR-#363 contract preserved)
# ────────────────────────────────────────────────────────────────────────────


class TestSingleL0Fallback:
    """When multi-line doesn't qualify, single-L0 (PR #363) still fires."""

    def test_single_l0_fires_when_multiline_insufficient(self, monkeypatch) -> None:
        """2 lines aligned (below multi-line threshold), large shift.
        Single-L0 applies; preserves PR #363's Instant-Crush-class fix.
        """
        from app.pipeline import lyrics_alignment

        rec = _LogRecorder()
        monkeypatch.setattr(lyrics_alignment, "log", rec)

        anchors, words = _consistent_aligned_track(line_count=2, shift_s=2.5)
        result = align_with_line_anchors(anchors, words, track_end_s=60.0)

        assert result.lines[0].start_s == pytest.approx(12.5, abs=1e-2)
        applied = rec.events_named("lyrics_alignment_reanchor_single_l0_applied")
        assert len(applied) == 1
        assert applied[0]["path"] == "single_l0"

    def test_single_l0_path_unchanged_at_3_aligned_lines_large_shift(self, monkeypatch) -> None:
        """3 eligible lines + large shift: multi-line median applies first
        (since 2.0 > 0.2 and spread = 0), single-L0 never runs. Line bounds
        produced are `LRC_anchor + shift` for start AND `next_anchor + shift
        - safety` for end (the standard apply-uniform-shift math).
        """
        from app.pipeline import lyrics_alignment

        rec = _LogRecorder()
        monkeypatch.setattr(lyrics_alignment, "log", rec)

        anchors, words = _consistent_aligned_track(line_count=3, shift_s=2.0)
        result = align_with_line_anchors(anchors, words, track_end_s=60.0)
        # anchors at [10, 15, 20], shift = 2.0
        assert result.lines[0].start_s == pytest.approx(12.0, abs=1e-2)
        # L0 end = next_anchor (15) + shift (2.0) - safety (0.05) = 16.95
        assert result.lines[0].end_s == pytest.approx(
            15.0 + 2.0 - _REANCHOR_NEXT_LINE_SAFETY_S, abs=1e-2
        )
        # multi-line applied (preempted single-L0)
        assert len(rec.events_named("lyrics_alignment_reanchor_multiline_applied")) == 1
        assert not rec.events_named("lyrics_alignment_reanchor_single_l0_applied")


# ────────────────────────────────────────────────────────────────────────────
# No-shift path (clean tracks)
# ────────────────────────────────────────────────────────────────────────────


class TestNoShift:
    """Clean tracks where neither path qualifies emit no_shift, leave
    line bounds at Whisper-derived values (per-PR-#363 contract)."""

    def test_no_shift_when_both_paths_skip(self, monkeypatch) -> None:
        """4 lines, tiny noise (~0.08s consistent). Multi-line skip
        (median < 0.2). Single-L0 skip (|shift| <= 1.0). Net: no_shift."""
        from app.pipeline import lyrics_alignment

        rec = _LogRecorder()
        monkeypatch.setattr(lyrics_alignment, "log", rec)

        anchors, words = _consistent_aligned_track(line_count=4, shift_s=0.08)
        align_with_line_anchors(anchors, words, track_end_s=60.0)

        no_shift = rec.events_named("lyrics_alignment_reanchor_no_shift")
        assert len(no_shift) == 1
        assert no_shift[0]["path"] == "none"


# ────────────────────────────────────────────────────────────────────────────
# Defensive bails
# ────────────────────────────────────────────────────────────────────────────


class TestDefensiveBails:
    """Edge-case inputs don't crash."""

    def test_empty_matched_counts_safe(self, monkeypatch) -> None:
        """Empty inputs return empty result — invariant held."""
        result = align_with_line_anchors([], [], track_end_s=10.0)
        assert result.lines == ()

    # Implausible-shift coverage for the multi-line path is delegated to
    # the single-L0 implausible test in test_lyrics_alignment_lrc_shift.py
    # (TestDefensiveBails::test_skip_on_implausible_shift). With N=1 aligned
    # line, the multi-line path can't fire (needs N>=3 eligible), so the
    # request falls through to the single-L0 guard. Constructing a synthetic
    # scenario where the multi-line median itself becomes implausible
    # requires shift > anchor_step (so window matching breaks down BEFORE
    # the implausibility guard can fire) AND track_dur < 3*shift — these
    # are mutually exclusive with the synthetic helpers in this file.
    # The multi-line implausible guard exists as defense-in-depth; in prod
    # it would only fire on a track where Whisper systematically hallucinates
    # AND the hallucination is consistent across N>=3 lines — extreme
    # enough that the L0 implausible bail is the primary defense.


# ────────────────────────────────────────────────────────────────────────────
# Constants sanity
# ────────────────────────────────────────────────────────────────────────────


class TestConstants:
    """Lock the user-chosen thresholds so a future drive-by edit can't
    silently widen / tighten them without test failure."""

    def test_min_eligible_lines_is_3(self) -> None:
        assert _MULTILINE_MIN_ELIGIBLE_LINES == 3

    def test_min_apply_shift_is_0_2(self) -> None:
        assert _MULTILINE_MIN_APPLY_SHIFT_S == pytest.approx(0.2)

    def test_max_mad_is_0_22(self) -> None:
        from app.pipeline.lyrics_alignment import (  # noqa: PLC0415
            _MULTILINE_MAX_MAD_S,
        )

        assert _MULTILINE_MAX_MAD_S == pytest.approx(0.22)

    def test_inlier_k_is_1_5(self) -> None:
        from app.pipeline.lyrics_alignment import (  # noqa: PLC0415
            _MULTILINE_INLIER_K,
        )

        assert _MULTILINE_INLIER_K == pytest.approx(1.5)

    def test_min_inliers_is_3(self) -> None:
        from app.pipeline.lyrics_alignment import (  # noqa: PLC0415
            _MULTILINE_MIN_INLIERS,
        )

        assert _MULTILINE_MIN_INLIERS == 3

    def test_matched_count_threshold_is_2(self) -> None:
        assert _MULTILINE_MATCHED_COUNT_THRESHOLD == 2


# ────────────────────────────────────────────────────────────────────────────
# Cache-bust contract
# ────────────────────────────────────────────────────────────────────────────


class TestPromptVersionBump:
    """Lock the prompt_version that invalidates stale cached lyric blobs."""

    def test_lyrics_extraction_prompt_version_bumped(self) -> None:
        from app.agents.lyrics import LyricsExtractionAgent

        assert (
            LyricsExtractionAgent.spec.prompt_version
            == "2026-06-06.repeated-chorus-prefix-lookback"
        )


# ────────────────────────────────────────────────────────────────────────────
# Prod regression: Overnight + The Bay
# ────────────────────────────────────────────────────────────────────────────


def _aligned_line_with_start(start_s: float, text: str = "x") -> AlignedLine:
    """Build a synthetic AlignedLine carrying just enough info for
    `_maybe_reanchor_to_lrc` to compute shifts. Per-word AlignedWord is
    a single token at the line's start_s — the re-anchor reads
    `line.start_s` (for shift detection) and `line.words[-1].end_s`
    (for last-line tail extension). Both are honored.
    """
    from app.pipeline.lyrics_alignment import AlignedWord  # noqa: PLC0415

    return AlignedLine(
        text=text,
        start_s=start_s,
        end_s=start_s + 1.0,
        words=(AlignedWord(text="x", start_s=start_s, end_s=start_s + 0.2),),
    )


class TestOvernightRegression:
    """Parcels - Overnight (track 8b36ec66) — prod-confirmed sub-second
    drift. Empirical shifts from prod cache (lyrics_cached.lines vs
    LRCLIB recording 1613166 anchors):
        L0..L4: [0.43, 0.85, 0.27, 0.71, 0.47]
        median = 0.47, MAD = 0.20

    Pre-median-path (PR #363 + earlier) WOULD NOT FIRE — single-L0 sees
    0.43s shift (below 1.0s threshold). Multi-line median with raw
    stdev<0.15 WOULD NOT FIRE either (stdev = 0.232). The MAD + inlier
    consensus design catches it: inlier band ±1.5*0.20 = ±0.30 around
    0.47, so [0.43, 0.27, 0.71, 0.47] are inliers and 0.85 (the Whisper
    jitter on L1) drops out. Refined median = (0.43 + 0.47)/2 = 0.45.
    """

    OVERNIGHT_ANCHORS = [
        SyncedLine(start_s=17.15, text="Go back I want"),
        SyncedLine(start_s=21.09, text="So bad to hold you back"),
        SyncedLine(start_s=25.15, text="It's all I've said and done"),
        SyncedLine(start_s=32.91, text="Far been, be gone"),
        SyncedLine(start_s=37.19, text="I was there as I wanna get"),
    ]
    OVERNIGHT_ALIGNED_STARTS = [17.58, 21.94, 25.42, 33.62, 37.66]

    def test_overnight_multi_line_median_fires(self, monkeypatch) -> None:
        """Replay the prod shift profile through `_maybe_reanchor_to_lrc`.
        Confirms the inlier-refined median fires and applies ~0.45s shift."""
        from app.pipeline import lyrics_alignment
        from app.pipeline.lyrics_alignment import _maybe_reanchor_to_lrc

        rec = _LogRecorder()
        monkeypatch.setattr(lyrics_alignment, "log", rec)

        aligned_lines = [_aligned_line_with_start(s) for s in self.OVERNIGHT_ALIGNED_STARTS]
        matched_counts = [2] * len(aligned_lines)  # all Strategy 1 / 2

        rebuilt = _maybe_reanchor_to_lrc(
            aligned_lines=aligned_lines,
            anchor_lines=self.OVERNIGHT_ANCHORS,
            track_end_s=219.648,
            whisper_words=[],
            matched_counts=matched_counts,
        )

        applied = rec.events_named("lyrics_alignment_reanchor_multiline_applied")
        assert len(applied) == 1, (
            f"Overnight regression: multi-line median MUST fire on this shift "
            f"profile. Events: {[e for _, e, _ in rec.events]}"
        )
        ev = applied[0]
        # MAD ≈ 0.20 — within the 0.22 cap by ~10% margin.
        assert ev["spread_mad_s"] == pytest.approx(0.20, abs=0.01)
        # Inlier filter drops 0.85; the other 4 cluster.
        assert ev["inlier_count"] == 4
        # Refined median = median of [0.43, 0.27, 0.71, 0.47] = (0.43+0.47)/2 = 0.45
        assert ev["refined_median_s"] == pytest.approx(0.45, abs=0.01)
        # L0 bound rewritten to anchor[0] + refined_median = 17.15 + 0.45 = 17.60
        assert rebuilt[0].start_s == pytest.approx(17.60, abs=0.01)
        # L4 bound rewritten too (uniform shift applies to ALL anchors).
        assert rebuilt[4].start_s == pytest.approx(37.64, abs=0.01)


class TestTheBayRegression:
    """Metronomy - The Bay (track a2e2e9d9) — prod-confirmed sub-second
    drift. Empirical shifts from prod cache vs LRCLIB recording 6066184:
        L0..L4: [0.58, 0.51, 0.82, 0.67, 0.61]
        median = 0.61, MAD = 0.06

    Tighter cluster than Overnight; would pass even a raw-stdev guard
    (stdev = 0.117). Locked here too so the regression is pinned for
    both user-reported tracks.
    """

    BAY_ANCHORS = [
        SyncedLine(start_s=31.92, text="You may have the money"),
        SyncedLine(start_s=34.31, text="But you've got to go"),
        SyncedLine(start_s=36.22, text="It's sensible, it's sensible"),
        SyncedLine(start_s=40.09, text="And those endless seasons"),
        SyncedLine(start_s=42.17, text="That go on and on"),
    ]
    BAY_ALIGNED_STARTS = [32.50, 34.82, 37.04, 40.76, 42.78]

    def test_the_bay_multi_line_median_fires(self, monkeypatch) -> None:
        from app.pipeline import lyrics_alignment
        from app.pipeline.lyrics_alignment import _maybe_reanchor_to_lrc

        rec = _LogRecorder()
        monkeypatch.setattr(lyrics_alignment, "log", rec)

        aligned_lines = [_aligned_line_with_start(s) for s in self.BAY_ALIGNED_STARTS]
        matched_counts = [2] * len(aligned_lines)

        rebuilt = _maybe_reanchor_to_lrc(
            aligned_lines=aligned_lines,
            anchor_lines=self.BAY_ANCHORS,
            track_end_s=231.912,
            whisper_words=[],
            matched_counts=matched_counts,
        )

        applied = rec.events_named("lyrics_alignment_reanchor_multiline_applied")
        assert len(applied) == 1
        ev = applied[0]
        assert ev["spread_mad_s"] == pytest.approx(0.06, abs=0.01)
        # Inlier band ±0.09 around 0.61: keeps [0.58, 0.67, 0.61], drops 0.51 + 0.82.
        assert ev["inlier_count"] == 3
        # Refined median = 0.61
        assert ev["refined_median_s"] == pytest.approx(0.61, abs=0.01)
        # L0 bound rewritten to 31.92 + 0.61 = 32.53
        assert rebuilt[0].start_s == pytest.approx(32.53, abs=0.01)
