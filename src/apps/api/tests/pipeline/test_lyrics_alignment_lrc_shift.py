"""LRC-anchor re-anchor regression suite.

When the detected audio-vs-LRC shift on the first matched line exceeds
`_AUDIO_SHIFT_THRESHOLD_S`, `align_with_line_anchors` rewrites every
`AlignedLine.start_s` / `end_s` to `LRC_anchor[i] + shift`. This fix
addresses the class where the audio cut does not match the LRC-indexed
cut — e.g. Instant Crush 339.79s official-video cut vs LRCLIB album cut
at 338.00s, where Whisper detects vocals 2-3s offset from LRC.

Per-word `AlignedWord.start_s` / `end_s` are intentionally NOT rewritten
— karaoke `\\kf` and per-word-pop consume per-word values, so those
styles are byte-identical regardless of whether re-anchor fires.

Tests:
  - Threshold: no re-anchor when shift < threshold (well-aligned tracks
    keep Whisper's per-line bounds).
  - Trigger: re-anchor fires when shift >= threshold; line bounds = LRC
    + shift; per-word values untouched.
  - Instant Crush regression: against the committed prod cached blob,
    L0-L3 spans match the user-confirmed corrected render to within 10ms.
  - Last-line handling: final line uses whisper-last-word + tail_pad,
    capped at track_end_s and floored at min_dur.
  - Length-mismatch defensive bail: skip re-anchor when aligned_count !=
    anchor_count to avoid mis-aligning lines that were skipped upstream.
  - Implausible-shift defensive bail: skip re-anchor when |shift| > 1/3
    of track duration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.lyrics_alignment import (
    _AUDIO_SHIFT_THRESHOLD_S,
    _REANCHOR_LAST_LINE_MIN_DUR_S,
    _REANCHOR_NEXT_LINE_SAFETY_S,
    align_with_line_anchors,
)
from app.services.lrclib_client import SyncedLine
from app.services.whisper_lyrics import WhisperWord

_INSTANT_CRUSH_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "lyrics_cached"
    / "instant_crush_lines_first_section.json"
)

# LRC anchors for Instant Crush (LRCLIB record 1909138, album cut).
_INSTANT_CRUSH_ANCHORS = [
    SyncedLine(start_s=26.31, text="I didn't want to be the one to forget"),
    SyncedLine(start_s=30.70, text="I thought of everything I'd never regret"),
    SyncedLine(start_s=34.93, text="A little time with you is all that I get"),
    SyncedLine(start_s=39.39, text="That's all we need because it's all we can take"),
    SyncedLine(start_s=43.76, text="One thing I never see the same when your 'round"),
]


def _load_instant_crush_whisper_words() -> list[WhisperWord]:
    raw = json.loads(_INSTANT_CRUSH_FIXTURE.read_text())
    return [
        WhisperWord(text=w["text"], start_s=w["start_s"], end_s=w["end_s"])
        for ln in raw["lines"]
        for w in ln["words"]
    ]


# ────────────────────────────────────────────────────────────────────────────
# Threshold behavior
# ────────────────────────────────────────────────────────────────────────────


class TestShiftThreshold:
    """Re-anchor fires only when |shift| exceeds the threshold."""

    def test_no_reanchor_when_shift_below_threshold(self) -> None:
        """Whisper detects L0 at LRC L0 + 0.3s (well below 1.0s threshold).
        Re-anchor must NOT fire; existing Whisper per-line bounds preserved.
        """
        anchors = [
            SyncedLine(start_s=10.0, text="hello world"),
            SyncedLine(start_s=15.0, text="foo bar"),
        ]
        # Whisper finds words ~0.3s after each anchor — small drift, well
        # below the 1.0s threshold. Whisper word ordering matches LRC.
        words = [
            WhisperWord(text="hello", start_s=10.3, end_s=10.6),
            WhisperWord(text="world", start_s=10.6, end_s=11.0),
            WhisperWord(text="foo", start_s=15.3, end_s=15.6),
            WhisperWord(text="bar", start_s=15.6, end_s=16.0),
        ]
        result = align_with_line_anchors(anchors, words, track_end_s=20.0)
        assert len(result.lines) == 2
        # No re-anchor: line bounds derive from Whisper word edges.
        assert result.lines[0].start_s == pytest.approx(10.3, abs=1e-3)
        assert result.lines[0].end_s == pytest.approx(11.0, abs=1e-3)
        assert result.lines[1].start_s == pytest.approx(15.3, abs=1e-3)
        assert result.lines[1].end_s == pytest.approx(16.0, abs=1e-3)

    def test_reanchor_fires_when_shift_above_threshold(self) -> None:
        """Whisper detects L0 at LRC L0 + 2.0s (above 1.0s threshold).
        Re-anchor must fire; every line's bounds rewritten to LRC + shift.
        Per-word `AlignedWord` timings unchanged.
        """
        anchors = [
            SyncedLine(start_s=10.0, text="hello world"),
            SyncedLine(start_s=15.0, text="foo bar"),
            SyncedLine(start_s=20.0, text="baz qux"),
        ]
        # Whisper finds vocals shifted +2.0s — confirms audio cut differs
        # from LRC's indexed cut.
        words = [
            WhisperWord(text="hello", start_s=12.0, end_s=12.3),
            WhisperWord(text="world", start_s=12.3, end_s=12.7),
            WhisperWord(text="foo", start_s=17.0, end_s=17.3),
            WhisperWord(text="bar", start_s=17.3, end_s=17.7),
            WhisperWord(text="baz", start_s=22.0, end_s=22.3),
            WhisperWord(text="qux", start_s=22.3, end_s=22.7),
        ]
        result = align_with_line_anchors(anchors, words, track_end_s=30.0)
        assert len(result.lines) == 3
        # Re-anchored: line.start = LRC_anchor + 2.0
        assert result.lines[0].start_s == pytest.approx(12.0, abs=1e-3)
        assert result.lines[1].start_s == pytest.approx(17.0, abs=1e-3)
        assert result.lines[2].start_s == pytest.approx(22.0, abs=1e-3)
        # Re-anchored: line.end = next_LRC_anchor + 2.0 - safety
        assert result.lines[0].end_s == pytest.approx(17.0 - _REANCHOR_NEXT_LINE_SAFETY_S, abs=1e-3)
        assert result.lines[1].end_s == pytest.approx(22.0 - _REANCHOR_NEXT_LINE_SAFETY_S, abs=1e-3)

    def test_per_word_timings_untouched_when_reanchor_fires(self) -> None:
        """Per-word `AlignedWord` timings must stay as Whisper produced
        them, even when line-level start/end are rewritten. This is the
        guarantee karaoke + per-word-pop renderers depend on."""
        anchors = [
            SyncedLine(start_s=10.0, text="hello"),
            SyncedLine(start_s=15.0, text="world"),
        ]
        words = [
            WhisperWord(text="hello", start_s=12.0, end_s=12.5),
            WhisperWord(text="world", start_s=17.0, end_s=17.5),
        ]
        result = align_with_line_anchors(anchors, words, track_end_s=20.0)
        assert len(result.lines) == 2
        # Line bounds re-anchored (+2.0 shift detected from L0).
        assert result.lines[0].start_s == pytest.approx(12.0, abs=1e-3)
        # But per-word `AlignedWord` keeps Whisper's exact values.
        l0_word = result.lines[0].words[0]
        assert l0_word.start_s == pytest.approx(12.0, abs=1e-3)
        assert l0_word.end_s == pytest.approx(12.5, abs=1e-3)


# ────────────────────────────────────────────────────────────────────────────
# Instant Crush regression
# ────────────────────────────────────────────────────────────────────────────


class TestInstantCrushRegression:
    """End-to-end regression against the prod cached blob. After re-anchor,
    line bounds must match the user-confirmed corrected render's targets
    (within 10ms tolerance — small per-line drift is OK because the
    trailing-interpolation guard may extend a line by up to
    `_REANCHOR_NEXT_LINE_SAFETY_S`)."""

    def test_lrc_shift_reanchors_to_corrected_render_targets(self) -> None:
        words = _load_instant_crush_whisper_words()
        result = align_with_line_anchors(_INSTANT_CRUSH_ANCHORS, words, track_end_s=339.792)
        assert len(result.lines) == 5

        # Per-line targets from the user-confirmed render (LRC + 2.57 shift).
        # Tolerance 0.06s — the trailing-interpolation guard may extend by up
        # to _REANCHOR_NEXT_LINE_SAFETY_S on lines where Whisper interpolated
        # past the LRC window.
        targets = [
            (28.88, 33.22),
            (33.27, 37.45),
            (37.50, 41.91),
            (41.96, 46.28),
            (46.33, None),  # final line — handled by last-line path
        ]
        tolerance = 0.06
        for i, (es, ee) in enumerate(targets):
            assert result.lines[i].start_s == pytest.approx(es, abs=tolerance), (
                f"L{i} start_s diverged from corrected-render target"
            )
            if ee is not None:
                assert result.lines[i].end_s == pytest.approx(ee, abs=tolerance), (
                    f"L{i} end_s diverged from corrected-render target"
                )

    def test_audio_shift_detected_correctly(self) -> None:
        """Detected shift = first_aligned.start_s − first_anchor.start_s
        = 28.880 − 26.31 = 2.57s. Above the 1.0s threshold — re-anchor fires.
        """
        words = _load_instant_crush_whisper_words()
        # L0's first Whisper word should be "I" at 28.88.
        assert words[0].text == "I"
        assert words[0].start_s == pytest.approx(28.88, abs=1e-3)
        shift = 28.88 - 26.31
        assert shift > _AUDIO_SHIFT_THRESHOLD_S, "test premise wrong"


# ────────────────────────────────────────────────────────────────────────────
# Defensive bails
# ────────────────────────────────────────────────────────────────────────────


class TestDefensiveBails:
    """Re-anchor must skip safely when inputs would produce bad output."""

    def test_skip_when_aligned_count_mismatches_anchor_count(self) -> None:
        """If `_align_within_window` skipped any anchor (empty text,
        malformed window), `aligned_lines.length < anchor_lines.length`
        and the i-th aligned line no longer corresponds to the i-th
        anchor. Re-anchor must bail rather than mis-align."""
        # 3 anchors but the middle one is empty — alignment will skip it.
        anchors = [
            SyncedLine(start_s=10.0, text="hello"),
            SyncedLine(start_s=15.0, text=""),  # empty → _align_within_window skips
            SyncedLine(start_s=20.0, text="world"),
        ]
        words = [
            WhisperWord(text="hello", start_s=12.0, end_s=12.5),
            WhisperWord(text="world", start_s=22.0, end_s=22.5),
        ]
        result = align_with_line_anchors(anchors, words, track_end_s=30.0)
        # Only 2 lines aligned. Length mismatch → no re-anchor → original
        # Whisper-derived bounds preserved.
        assert len(result.lines) == 2
        assert result.lines[0].start_s == pytest.approx(12.0, abs=1e-3)
        # If re-anchor had fired, L1 (the "world" anchor) would have been
        # mapped to anchors[1] (the empty one at 15.0), producing 15.0+shift
        # for its start. Bail prevents that.
        assert result.lines[1].start_s != pytest.approx(17.0, abs=0.1)

    def test_skip_on_implausible_shift(self) -> None:
        """A shift larger than 1/3 of track duration is almost certainly
        a Whisper hallucination on L0. Bail to preserve sane bounds."""
        anchors = [SyncedLine(start_s=5.0, text="hello")]
        # Whisper "finds" L0 at 60s on a 90s track — that's 55s shift,
        # well past 1/3 of track duration (30s).
        words = [WhisperWord(text="hello", start_s=60.0, end_s=60.5)]
        result = align_with_line_anchors(anchors, words, track_end_s=90.0)
        assert len(result.lines) == 1
        # No re-anchor: preserve Whisper's bounds (even though they're
        # weird — at least we're consistent with the per-word stream).
        assert result.lines[0].start_s == pytest.approx(60.0, abs=1e-3)


# ────────────────────────────────────────────────────────────────────────────
# Last-line handling
# ────────────────────────────────────────────────────────────────────────────


class TestLastLineHandling:
    """The final line has no `next anchor` to cap its end. Re-anchor uses
    Whisper's last-word end + tail_pad, capped at track_end_s, floored at
    `_REANCHOR_LAST_LINE_MIN_DUR_S` past start."""

    def test_last_line_uses_whisper_last_word_end(self) -> None:
        anchors = [
            SyncedLine(start_s=10.0, text="hello"),
            SyncedLine(start_s=15.0, text="goodbye"),  # last line
        ]
        words = [
            WhisperWord(text="hello", start_s=12.0, end_s=12.5),
            # "goodbye" sung from 17.0 to 19.0 (2s sustain).
            WhisperWord(text="goodbye", start_s=17.0, end_s=19.0),
        ]
        result = align_with_line_anchors(anchors, words, track_end_s=25.0)
        assert len(result.lines) == 2
        # Re-anchored: L1 start = LRC L1 + 2.0 = 17.0
        assert result.lines[1].start_s == pytest.approx(17.0, abs=1e-3)
        # L1 end = whisper_last_word_end (19.0) + tail_pad (0.5) = 19.5,
        # capped at track_end_s (25.0), floored at start + min_dur (20.0).
        # Floor wins: 20.0.
        assert result.lines[1].end_s == pytest.approx(
            17.0 + _REANCHOR_LAST_LINE_MIN_DUR_S, abs=1e-3
        )

    def test_last_line_capped_at_track_end(self) -> None:
        """If whisper's last word + tail_pad would extend past track_end_s,
        cap at track_end_s."""
        anchors = [
            SyncedLine(start_s=10.0, text="hello"),
            SyncedLine(start_s=15.0, text="goodbye"),
        ]
        words = [
            WhisperWord(text="hello", start_s=12.0, end_s=12.5),
            WhisperWord(text="goodbye", start_s=17.0, end_s=22.0),  # held vowel
        ]
        result = align_with_line_anchors(anchors, words, track_end_s=21.0)
        # whisper_last_end + tail_pad = 22.0 + 0.5 = 22.5
        # track_end_s = 21.0 → cap binds
        # start + min_dur = 17.0 + 3.0 = 20.0 (floor < cap)
        # Final: min(22.5, 21.0) = 21.0
        assert result.lines[1].end_s == pytest.approx(21.0, abs=1e-3)
