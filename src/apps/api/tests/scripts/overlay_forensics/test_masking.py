"""Tests for scripts/overlay_forensics/masking.py."""
from __future__ import annotations

import numpy as np

from scripts.overlay_forensics.masking import (
    BBox,
    all_pixels_bbox,
    color_delta_e,
    largest_blob_bbox,
    mask_by_color,
    mask_white,
    sample_color,
    serif_score,
    stroke_width_ratio,
)


def _frame(h: int, w: int, fill: tuple[int, int, int] = (0, 0, 0)) -> np.ndarray:
    f = np.zeros((h, w, 3), dtype=np.uint8)
    f[..., 0] = fill[0]
    f[..., 1] = fill[1]
    f[..., 2] = fill[2]
    return f


def test_mask_by_color_matches_within_tolerance():
    frame = _frame(40, 40, fill=(10, 10, 10))
    frame[10:20, 5:25] = (244, 208, 63)  # maize block
    mask = mask_by_color(frame, (244, 208, 63), per_channel_tol=10)
    assert mask.sum() == 10 * 20
    # Outside the block, no pixels match.
    assert mask[0:5, :].sum() == 0


def test_mask_by_color_respects_tolerance_edges():
    frame = _frame(20, 20, fill=(0, 0, 0))
    frame[5:10, 5:10] = (244, 208, 63)
    frame[10:15, 10:15] = (255, 220, 80)  # slightly off-target, still within tol=35
    mask = mask_by_color(frame, (244, 208, 63), per_channel_tol=35)
    assert mask[5:10, 5:10].all()
    assert mask[10:15, 10:15].all()


def test_mask_by_color_rejects_non_rgb_shape():
    import pytest
    with pytest.raises(ValueError):
        mask_by_color(np.zeros((10, 10), dtype=np.uint8), (255, 255, 255))


def test_mask_white_excludes_yellow_pixels():
    """White mask must not light up on maize pixels — R+G high but B is 63."""
    frame = _frame(20, 20, fill=(0, 0, 0))
    frame[2:8, 2:8] = (255, 255, 255)
    frame[10:16, 10:16] = (244, 208, 63)
    mask = mask_white(frame, min_luma=235)
    assert mask[2:8, 2:8].all()
    assert not mask[10:16, 10:16].any()


def test_largest_blob_bbox_finds_largest_blob_when_scipy_present():
    # Two blobs: small at (0..2), large at (5..18). Largest must win.
    mask = np.zeros((20, 20), dtype=bool)
    mask[0:3, 0:3] = True       # 9 pixels
    mask[5:19, 5:19] = True     # 196 pixels
    bbox = largest_blob_bbox(mask, min_pixels=50)
    assert bbox is not None
    assert bbox.x_min == 5
    assert bbox.y_min == 5
    assert bbox.x_max == 18
    assert bbox.y_max == 18


def test_largest_blob_bbox_returns_none_below_min_pixels():
    mask = np.zeros((20, 20), dtype=bool)
    mask[0:3, 0:3] = True
    assert largest_blob_bbox(mask, min_pixels=50) is None


def test_largest_blob_bbox_empty():
    mask = np.zeros((10, 10), dtype=bool)
    assert largest_blob_bbox(mask) is None


def test_all_pixels_bbox_returns_tight_box():
    mask = np.zeros((20, 20), dtype=bool)
    mask[3, 4] = True
    mask[15, 14] = True
    bbox = all_pixels_bbox(mask)
    assert bbox == BBox(x_min=4, y_min=3, x_max=14, y_max=15)


def test_all_pixels_bbox_empty():
    assert all_pixels_bbox(np.zeros((5, 5), dtype=bool)) is None


def test_stroke_width_ratio_bold_vs_thin():
    """Bold strokes inside a same-height glyph score higher than thin strokes.

    We mock a glyph as a 20px-tall letter outline. Bold = 5px stroke
    thickness; thin = 1px stroke thickness. Both occupy the same overall
    bbox (so bbox.height is the divisor we normalize by).
    """
    H, W = 30, 30
    # Both glyphs are 20px tall, 20px wide (rows 5..24, cols 5..24).
    bold = np.zeros((H, W), dtype=bool)
    bold[5:25, 5:10] = True   # 5px-wide left vertical bar
    bold[5:25, 20:25] = True  # 5px-wide right vertical bar
    bold[5:10, 5:25] = True   # 5px-tall top bar
    bold[20:25, 5:25] = True  # 5px-tall bottom bar
    bold_bbox = BBox(x_min=5, y_min=5, x_max=24, y_max=24)
    thin = np.zeros((H, W), dtype=bool)
    thin[5:25, 5:6] = True    # 1px-wide left vertical
    thin[5:25, 24:25] = True  # 1px-wide right vertical
    thin[5:6, 5:25] = True    # 1px-tall top
    thin[24:25, 5:25] = True  # 1px-tall bottom
    thin_bbox = BBox(x_min=5, y_min=5, x_max=24, y_max=24)
    assert stroke_width_ratio(bold, bold_bbox) > stroke_width_ratio(thin, thin_bbox)


def test_serif_score_bounded_0_to_1():
    mask = np.zeros((50, 50), dtype=bool)
    mask[20:30, 10:40] = True
    bbox = BBox(x_min=10, y_min=20, x_max=39, y_max=29)
    s = serif_score(mask, bbox)
    assert 0.0 <= s <= 1.0


def test_color_delta_e_zero_for_identical():
    assert color_delta_e((100, 100, 100), (100, 100, 100)) == 0.0


def test_color_delta_e_positive_for_different():
    assert color_delta_e((0, 0, 0), (255, 255, 255)) > 100


def test_sample_color_returns_mean_of_masked_pixels():
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    frame[2:5, 2:5] = (200, 100, 50)
    mask = np.zeros((10, 10), dtype=bool)
    mask[2:5, 2:5] = True
    assert sample_color(frame, mask) == (200, 100, 50)


def test_sample_color_empty_mask_returns_black():
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    mask = np.zeros((10, 10), dtype=bool)
    assert sample_color(frame, mask) == (0, 0, 0)


def test_bbox_relative_coords():
    bb = BBox(x_min=100, y_min=200, x_max=199, y_max=299)
    rel = bb.to_relative(1000, 1000)
    assert rel.x_frac == 0.1
    assert rel.y_frac == 0.2
    assert rel.w_frac == 0.1
    assert rel.h_frac == 0.1
    assert abs(rel.cx_frac - 0.15) < 1e-6
    assert abs(rel.cy_frac - 0.25) < 1e-6
