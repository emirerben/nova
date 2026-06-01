"""Unit tests for overlay_sizing.compute_overlay_size + clamp_intro_px.

The fit search lazy-imports skia inside compute_overlay_size; these run in the
test env where skia is installed (same as the skia renderer tests). The contract:
size is ALWAYS computed within [MIN_INTRO_PX, MAX_INTRO_PX] — never a hardcoded
default — and tracks empty space + density.
"""

import pytest

from app.pipeline.overlay_sizing import (
    MAX_INTRO_PX,
    MIN_INTRO_PX,
    clamp_intro_px,
    compute_overlay_size,
)


def test_calm_open_frame_sizes_larger_than_busy_tight_frame():
    text = "I quit my job"
    calm = compute_overlay_size(
        text,
        font_family="Bricolage Grotesque",
        safe_zone={"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.5},
        visual_density=1.0,
    )
    busy = compute_overlay_size(
        text,
        font_family="Bricolage Grotesque",
        safe_zone={"x": 0.3, "y": 0.6, "w": 0.4, "h": 0.18},
        visual_density=9.0,
    )
    assert calm > busy
    assert MIN_INTRO_PX <= busy <= MAX_INTRO_PX
    assert MIN_INTRO_PX <= calm <= MAX_INTRO_PX


def test_longer_text_sizes_down_vs_short_in_same_box():
    # A tight box so the long line genuinely can't fit at the editorial cap and
    # must shrink below the short line (in a roomy box both just hit the cap).
    box = {"x": 0.2, "y": 0.4, "w": 0.6, "h": 0.15}
    short = compute_overlay_size(
        "go", font_family="Bricolage Grotesque", safe_zone=box, visual_density=2.0
    )
    long = compute_overlay_size(
        "when in rome you simply must follow every single rule they have here",
        font_family="Bricolage Grotesque",
        safe_zone=box,
        visual_density=2.0,
    )
    assert long < short


@pytest.mark.parametrize(
    "bad_zone", [None, {}, {"w": 0}, {"w": 1.2, "h": 0.5}, {"w": "x", "h": "y"}, "junk"]
)
def test_degraded_safe_zone_still_computes_within_envelope(bad_zone):
    # Best-effort: a missing/malformed safe zone must NOT crash and must NOT fall
    # back to a hardcoded constant — it computes against a full-width fallback box.
    px = compute_overlay_size(
        "here is the truth", font_family="DM Serif Display", safe_zone=bad_zone, visual_density=5.0
    )
    assert MIN_INTRO_PX <= px <= MAX_INTRO_PX


def test_empty_text_returns_min():
    assert compute_overlay_size("", font_family=None) == MIN_INTRO_PX
    assert compute_overlay_size("   ", font_family="Syne") == MIN_INTRO_PX


def test_unknown_font_falls_back_without_crashing():
    px = compute_overlay_size(
        "hello", font_family="NoSuchFont 999", safe_zone={"x": 0, "y": 0, "w": 0.8, "h": 0.4}
    )
    assert MIN_INTRO_PX <= px <= MAX_INTRO_PX


def test_density_monotonic_within_same_box():
    box = {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.6}
    sizes = [
        compute_overlay_size("watch this", font_family="Anton", safe_zone=box, visual_density=d)
        for d in (0.0, 5.0, 10.0)
    ]
    # Higher density never yields a LARGER size (density only caps the ceiling).
    assert sizes[0] >= sizes[1] >= sizes[2]


@pytest.mark.parametrize(
    "raw,expected",
    [
        (10, MIN_INTRO_PX),
        (9999, MAX_INTRO_PX),
        (64, 64),  # in-range value passes through
        (MIN_INTRO_PX, MIN_INTRO_PX),
        ("72", 72),  # numeric string, in range
    ],
)
def test_clamp_intro_px(raw, expected):
    assert clamp_intro_px(raw) == expected


def test_clamp_intro_px_rejects_junk():
    with pytest.raises((ValueError, TypeError)):
        clamp_intro_px("not-a-number")
