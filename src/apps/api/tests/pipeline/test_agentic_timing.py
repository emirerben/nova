"""Unit tests for app.pipeline.agentic_timing.

Covers the dispatch matrix for both helpers across the (is_agentic, pct-present)
combinations plus the edge cases (out-of-range pct, partial pct, 0-second user
total). Pure Python — no Gemini, no fixtures, no I/O.
"""

from __future__ import annotations

import pytest

from app.pipeline.agentic_timing import (
    resolve_overlay_window,
    resolve_slot_duration,
)


class TestResolveSlotDuration:
    def test_classic_returns_frozen_seconds(self):
        slot = {"target_duration_s": 5.0, "target_duration_pct": 0.5}
        dur = resolve_slot_duration(slot, is_agentic=False, user_total_dur_s=20.0)
        # Classic path ignores pct entirely.
        assert dur == 5.0

    def test_agentic_with_pct_scales_to_user_total(self):
        slot = {"target_duration_s": 5.0, "target_duration_pct": 0.25}
        dur = resolve_slot_duration(slot, is_agentic=True, user_total_dur_s=40.0)
        assert dur == pytest.approx(10.0)  # 0.25 * 40

    def test_agentic_with_pct_handles_other_user_durations(self):
        slot = {"target_duration_s": 22.0, "target_duration_pct": 1.0}
        # 6-second user clip: slot should scale down to 6.
        assert resolve_slot_duration(slot, is_agentic=True, user_total_dur_s=6.0) == pytest.approx(
            6.0
        )
        # 30-second user clip: slot should scale up to 30.
        assert resolve_slot_duration(slot, is_agentic=True, user_total_dur_s=30.0) == pytest.approx(
            30.0
        )

    def test_agentic_without_pct_falls_back_to_seconds(self):
        """Lazy-migration fallback: legacy agentic recipe lacks pct."""
        slot = {"target_duration_s": 5.0}  # no target_duration_pct
        dur = resolve_slot_duration(slot, is_agentic=True, user_total_dur_s=40.0)
        assert dur == 5.0

    def test_agentic_with_pct_out_of_range_falls_back(self):
        slot = {"target_duration_s": 5.0, "target_duration_pct": 1.5}
        dur = resolve_slot_duration(slot, is_agentic=True, user_total_dur_s=40.0)
        assert dur == 5.0  # falls back to seconds, doesn't compute 60.0

    def test_agentic_with_pct_zero_falls_back(self):
        slot = {"target_duration_s": 5.0, "target_duration_pct": 0.0}
        dur = resolve_slot_duration(slot, is_agentic=True, user_total_dur_s=40.0)
        # 0 is invalid (must be > 0), falls back.
        assert dur == 5.0

    def test_agentic_with_non_numeric_pct_falls_back(self):
        slot = {"target_duration_s": 5.0, "target_duration_pct": "not-a-number"}
        dur = resolve_slot_duration(slot, is_agentic=True, user_total_dur_s=40.0)
        assert dur == 5.0

    def test_agentic_with_nan_pct_falls_back(self):
        """F3 fix: NaN passes simple range checks because all NaN comparisons return False."""
        slot = {"target_duration_s": 5.0, "target_duration_pct": float("nan")}
        dur = resolve_slot_duration(slot, is_agentic=True, user_total_dur_s=40.0)
        assert dur == 5.0  # not NaN

    def test_agentic_with_inf_pct_falls_back(self):
        slot = {"target_duration_s": 5.0, "target_duration_pct": float("inf")}
        dur = resolve_slot_duration(slot, is_agentic=True, user_total_dur_s=40.0)
        assert dur == 5.0

    def test_agentic_with_zero_user_total_falls_back_critical_gap(self):
        """0-second user clip set logs loudly and returns frozen value.

        Documented in plan as critical-gap requiring an upstream guard
        (matcher should refuse a 0-duration clip), but resolve_slot_duration
        must not silently produce a 0-duration slot.
        """
        slot = {"target_duration_s": 5.0, "target_duration_pct": 0.5}
        dur = resolve_slot_duration(slot, is_agentic=True, user_total_dur_s=0.0)
        assert dur == 5.0  # not 0


class TestResolveOverlayWindow:
    def test_classic_returns_frozen_seconds(self):
        ov = {
            "start_s": 2.0,
            "end_s": 5.0,
            "start_pct": 0.1,
            "end_pct": 0.5,
        }
        start, end = resolve_overlay_window(ov, slot_actual_dur=20.0, is_agentic=False)
        # Classic ignores pct entirely.
        assert start == 2.0
        assert end == 5.0

    def test_agentic_with_pct_scales(self):
        ov = {
            "start_s": 5.7,
            "end_s": 22.0,
            "start_pct": 0.26,
            "end_pct": 1.0,
        }
        # 6-second user slot: text should appear at ~1.56s → 6.0s.
        start, end = resolve_overlay_window(ov, slot_actual_dur=6.0, is_agentic=True)
        assert start == pytest.approx(1.56)
        assert end == pytest.approx(6.0)

        # 30-second user slot: same overlay at 7.8s → 30.0s.
        start, end = resolve_overlay_window(ov, slot_actual_dur=30.0, is_agentic=True)
        assert start == pytest.approx(7.8)
        assert end == pytest.approx(30.0)

    def test_agentic_without_pct_falls_back_to_seconds(self):
        """Lazy-migration: agentic recipe predating pct schema."""
        ov = {"start_s": 2.0, "end_s": 5.0}  # no pct
        start, end = resolve_overlay_window(ov, slot_actual_dur=20.0, is_agentic=True)
        assert start == 2.0
        assert end == 5.0

    def test_agentic_with_only_start_pct_falls_back(self):
        """Partial pct (one of two) → both-required violation, falls back."""
        ov = {"start_s": 2.0, "end_s": 5.0, "start_pct": 0.1}  # no end_pct
        start, end = resolve_overlay_window(ov, slot_actual_dur=20.0, is_agentic=True)
        assert start == 2.0
        assert end == 5.0

    def test_agentic_with_pct_out_of_range_falls_back(self):
        ov = {
            "start_s": 2.0,
            "end_s": 5.0,
            "start_pct": -0.1,
            "end_pct": 1.5,
        }
        start, end = resolve_overlay_window(ov, slot_actual_dur=20.0, is_agentic=True)
        assert start == 2.0
        assert end == 5.0

    def test_agentic_with_inverted_pct_falls_back(self):
        """start_pct >= end_pct violates monotonicity, falls back."""
        ov = {
            "start_s": 2.0,
            "end_s": 5.0,
            "start_pct": 0.6,
            "end_pct": 0.4,
        }
        start, end = resolve_overlay_window(ov, slot_actual_dur=20.0, is_agentic=True)
        assert start == 2.0
        assert end == 5.0

    def test_agentic_with_non_numeric_pct_falls_back(self):
        ov = {
            "start_s": 2.0,
            "end_s": 5.0,
            "start_pct": "x",
            "end_pct": "y",
        }
        start, end = resolve_overlay_window(ov, slot_actual_dur=20.0, is_agentic=True)
        assert start == 2.0
        assert end == 5.0

    def test_agentic_with_nan_pct_falls_back(self):
        """F3 fix mirrored for overlay window resolution."""
        ov = {
            "start_s": 2.0,
            "end_s": 5.0,
            "start_pct": float("nan"),
            "end_pct": 0.5,
        }
        start, end = resolve_overlay_window(ov, slot_actual_dur=20.0, is_agentic=True)
        assert start == 2.0
        assert end == 5.0

    def test_agentic_with_inf_pct_falls_back(self):
        ov = {
            "start_s": 2.0,
            "end_s": 5.0,
            "start_pct": 0.1,
            "end_pct": float("inf"),
        }
        start, end = resolve_overlay_window(ov, slot_actual_dur=20.0, is_agentic=True)
        assert start == 2.0
        assert end == 5.0

    def test_full_slot_overlay(self):
        """An overlay covering the full slot in pct should equal slot duration."""
        ov = {"start_s": 0.0, "end_s": 22.0, "start_pct": 0.0, "end_pct": 1.0}
        start, end = resolve_overlay_window(ov, slot_actual_dur=15.0, is_agentic=True)
        assert start == 0.0
        assert end == pytest.approx(15.0)
