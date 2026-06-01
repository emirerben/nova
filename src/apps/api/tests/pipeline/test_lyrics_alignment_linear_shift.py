"""Linear LRCLIB re-anchor regression suite.

The uniform median path handles constant audio-vs-LRCLIB offsets. This file
locks the next class: progressively growing drift where the correct shift is
`intercept + slope * anchor_time`.
"""

from __future__ import annotations

import pytest

from app.pipeline.lyrics_alignment import (
    _LINEAR_MAX_RESID_MAD_S,
    _LINEAR_MIN_ELIGIBLE_LINES,
    _LINEAR_MIN_SLOPE,
    _LINEAR_MIN_SPAN_FRAC,
    _REANCHOR_NEXT_LINE_SAFETY_S,
    align_with_line_anchors,
    settings,
)
from app.services.lrclib_client import SyncedLine
from app.services.whisper_lyrics import WhisperWord


class _LogRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.events.append(("info", event, dict(kwargs)))

    def warning(self, event: str, **kwargs: object) -> None:
        self.events.append(("warning", event, dict(kwargs)))

    def events_named(self, name: str) -> list[dict]:
        return [data for _, ev, data in self.events if ev == name]


def _linear_track(
    *,
    line_count: int = 6,
    intercept: float = 0.10,
    slope: float = 0.02,
    outlier_index: int | None = None,
    outlier_delta: float = 0.0,
) -> tuple[list[SyncedLine], list[WhisperWord]]:
    anchors: list[SyncedLine] = []
    words: list[WhisperWord] = []
    for i in range(line_count):
        anchor_t = i * 10.0
        shift = intercept + slope * anchor_t
        if outlier_index is not None and i == outlier_index:
            shift += outlier_delta
        word_t = anchor_t + shift
        anchors.append(SyncedLine(start_s=anchor_t, text=f"alpha{i} beta{i}"))
        words.append(WhisperWord(text=f"alpha{i}", start_s=word_t, end_s=word_t + 0.2))
        words.append(WhisperWord(text=f"beta{i}", start_s=word_t + 0.2, end_s=word_t + 0.4))
    return anchors, words


def test_growing_drift_applies_per_line_linear_shift(monkeypatch) -> None:
    """The first line's end uses the next anchor's larger predicted shift.

    Old uniform median would end L0 at `10 + 0.5 - safety = 10.45`.
    Linear re-anchor ends it at `10 + 0.3 - safety = 10.25`.
    """
    from app.pipeline import lyrics_alignment

    rec = _LogRecorder()
    monkeypatch.setattr(lyrics_alignment, "log", rec)

    anchors, words = _linear_track()
    result = align_with_line_anchors(anchors, words, track_end_s=60.0)

    assert result.lines[0].end_s == pytest.approx(
        10.0 + 0.30 - _REANCHOR_NEXT_LINE_SAFETY_S, abs=1e-2
    )
    assert result.lines[4].end_s == pytest.approx(
        50.0 + 1.10 - _REANCHOR_NEXT_LINE_SAFETY_S, abs=1e-2
    )

    applied = rec.events_named("lyrics_alignment_reanchor_linear_applied")
    assert len(applied) == 1
    assert applied[0]["slope"] == pytest.approx(0.02, abs=1e-4)
    assert applied[0]["intercept"] == pytest.approx(0.10, abs=1e-2)
    assert not rec.events_named("lyrics_alignment_reanchor_multiline_applied")


def test_clean_track_skips_linear_path(monkeypatch) -> None:
    """Flat tiny drift is a clean track: linear skips, uniform paths skip."""
    from app.pipeline import lyrics_alignment

    rec = _LogRecorder()
    monkeypatch.setattr(lyrics_alignment, "log", rec)

    anchors, words = _linear_track(intercept=0.08, slope=0.0)
    result = align_with_line_anchors(anchors, words, track_end_s=60.0)

    assert result.lines[0].end_s == pytest.approx(0.48, abs=1e-2)
    skipped = rec.events_named("lyrics_alignment_reanchor_linear_skipped")
    assert any(ev["reason"] == "slope_too_small" for ev in skipped)
    assert rec.events_named("lyrics_alignment_reanchor_no_shift")


def test_single_outlier_does_not_tilt_fit(monkeypatch) -> None:
    """Theil-Sen keeps the slope stable with one hallucinated onset."""
    from app.pipeline import lyrics_alignment

    rec = _LogRecorder()
    monkeypatch.setattr(lyrics_alignment, "log", rec)

    anchors, words = _linear_track(line_count=7, outlier_index=3, outlier_delta=3.0)
    result = align_with_line_anchors(anchors, words, track_end_s=70.0)

    assert result.lines[0].end_s == pytest.approx(
        10.0 + 0.30 - _REANCHOR_NEXT_LINE_SAFETY_S, abs=1e-2
    )
    applied = rec.events_named("lyrics_alignment_reanchor_linear_applied")
    assert len(applied) == 1
    assert applied[0]["slope"] == pytest.approx(0.02, abs=1e-4)
    assert applied[0]["resid_mad_s"] == pytest.approx(0.0, abs=1e-3)


def test_kill_switch_reproduces_uniform_median_path(monkeypatch) -> None:
    """Flag off skips Path 0 and preserves the old uniform fallback result."""
    from app.pipeline import lyrics_alignment

    anchors, words = _linear_track()

    monkeypatch.setattr(settings, "lyric_linear_reanchor_enabled", True)
    linear = align_with_line_anchors(anchors, words, track_end_s=60.0)

    rec = _LogRecorder()
    monkeypatch.setattr(lyrics_alignment, "log", rec)
    monkeypatch.setattr(settings, "lyric_linear_reanchor_enabled", False)
    uniform = align_with_line_anchors(anchors, words, track_end_s=60.0)

    assert linear.lines[0].end_s == pytest.approx(10.25, abs=1e-2)
    assert uniform.lines[0].end_s == pytest.approx(10.45, abs=1e-2)
    assert any(
        ev["reason"] == "disabled_by_flag"
        for ev in rec.events_named("lyrics_alignment_reanchor_linear_skipped")
    )
    assert rec.events_named("lyrics_alignment_reanchor_multiline_applied")


def test_linear_constants_are_pinned() -> None:
    assert _LINEAR_MIN_ELIGIBLE_LINES == 6
    assert _LINEAR_MIN_SPAN_FRAC == pytest.approx(0.30)
    assert _LINEAR_MIN_SLOPE == pytest.approx(0.01)
    assert _LINEAR_MAX_RESID_MAD_S == pytest.approx(0.15)
