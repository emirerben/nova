"""Tests for scripts/overlay_forensics/safe_crop.py."""
from __future__ import annotations

from scripts.overlay_forensics.masking import BBox
from scripts.overlay_forensics.safe_crop import (
    SAFE_AREA_X_MAX,
    SAFE_AREA_X_MIN,
    project_relative_to_9x16,
    project_to_9x16,
)

# Recipe geometry: 1024 × 576 (16:9).
# 9:16 center-crop width: 576 * 9/16 = 324px.
# crop_x_start = (1024 - 324) / 2 = 350; crop_x_end = 674.


def test_centroid_dead_center_survives():
    bb = BBox(x_min=480, y_min=200, x_max=540, y_max=260)  # cx=510
    proj = project_to_9x16(bb, source_w=1024, source_h=576)
    assert proj.survives is True
    # cx=510, mapped to (510-350)/324 ≈ 0.494
    assert proj.projected_x_frac is not None
    assert abs(proj.projected_x_frac - 0.494) < 0.01


def test_centroid_at_x_frac_0_25_in_recipe_falls_outside_crop():
    # cx = 256px (x_frac 0.25 in 1024-wide), well left of crop_x_start=350.
    bb = BBox(x_min=200, y_min=200, x_max=312, y_max=260)  # cx=256
    proj = project_to_9x16(bb, source_w=1024, source_h=576)
    assert proj.survives is False
    assert proj.projected_x_frac is None
    assert "outside center-crop band" in proj.note


def test_centroid_at_x_frac_0_70_in_recipe_falls_outside_crop():
    # cx ≈ 717, right of crop_x_end=674.
    bb = BBox(x_min=685, y_min=200, x_max=749, y_max=260)  # cx=717
    proj = project_to_9x16(bb, source_w=1024, source_h=576)
    assert proj.survives is False


def test_bbox_extending_past_crop_edge_flagged_even_if_centroid_inside():
    # cx = 660 (inside), but x_max = 680 > crop_x_end=674.
    bb = BBox(x_min=640, y_min=200, x_max=680, y_max=260)
    proj = project_to_9x16(bb, source_w=1024, source_h=576)
    assert proj.survives is False
    assert "extends past" in proj.note


def test_source_already_vertical_treated_as_noop_crop():
    bb = BBox(x_min=900, y_min=100, x_max=950, y_max=200)
    proj = project_to_9x16(bb, source_w=1080, source_h=1920)
    assert proj.survives is True
    # x_frac ≈ 925/1080 ≈ 0.857 — preserved, not mangled.
    assert proj.projected_x_frac is not None
    assert abs(proj.projected_x_frac - 925 / 1080) < 1e-3


def test_safe_area_violation_inside_crop():
    # Build a bbox whose centroid is inside the crop band but lands at
    # projected x_frac > 0.95 — i.e. against the right edge of the 9:16 frame.
    # crop_x_end=674; want cx near 674 → x_frac = (674-350)/324 = 1.0.
    bb = BBox(x_min=665, y_min=200, x_max=673, y_max=260)  # cx ≈ 669
    proj = project_to_9x16(bb, source_w=1024, source_h=576)
    assert proj.survives is True  # bbox inside band
    assert proj.projected_x_frac is not None
    assert proj.projected_x_frac > SAFE_AREA_X_MAX
    assert "safe area" in proj.note


def test_safe_area_within_bounds_clean_note():
    bb = BBox(x_min=480, y_min=200, x_max=540, y_max=260)
    proj = project_to_9x16(bb, source_w=1024, source_h=576)
    assert proj.survives is True
    assert "within safe area" in proj.note


def test_project_relative_helper_matches_pixel_version():
    bb = BBox(x_min=480, y_min=200, x_max=540, y_max=260)
    pixel_proj = project_to_9x16(bb, 1024, 576)
    rel = bb.to_relative(1024, 576)
    rel_proj = project_relative_to_9x16(rel, source_aspect_w_over_h=1024 / 576)
    assert pixel_proj.survives == rel_proj.survives
    # Floating-point math may differ slightly because the helper rebuilds
    # in a 1000-tall virtual frame, so allow a 0.01 tolerance.
    assert pixel_proj.projected_x_frac is not None
    assert rel_proj.projected_x_frac is not None
    assert abs(pixel_proj.projected_x_frac - rel_proj.projected_x_frac) < 0.01


def test_safe_area_minimum_constant_is_documented():
    """Lock the safe-area constants — changing them silently would shift
    every diff result. If you intend to change, update tests too."""
    assert SAFE_AREA_X_MIN == 0.05
    assert SAFE_AREA_X_MAX == 0.95
