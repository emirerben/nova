"""Unit tests for text_overlay_scoring helpers.

These cover the math underneath the template_text eval — IoU geometry, color
distance, greedy bipartite matching, and the end-to-end score_overlays
aggregation. The math is not load-bearing for production (it only feeds the
LLM judge prompt), but a subtle bug here would silently inflate or deflate
every eval score until someone noticed by hand.
"""

from __future__ import annotations

import math

import pytest

from .text_overlay_scoring import (
    COLOR_MATCH_DELTA_E_THRESHOLD,
    color_delta_e,
    greedy_match,
    score_overlays,
    spatial_iou,
    temporal_iou,
)


# ── temporal_iou ─────────────────────────────────────────────────────────────


def test_temporal_iou_identical_intervals_is_one() -> None:
    assert temporal_iou(0.0, 5.0, 0.0, 5.0) == pytest.approx(1.0)


def test_temporal_iou_disjoint_is_zero() -> None:
    assert temporal_iou(0.0, 1.0, 2.0, 3.0) == 0.0
    assert temporal_iou(2.0, 3.0, 0.0, 1.0) == 0.0


def test_temporal_iou_abutting_is_zero() -> None:
    # End-to-start touching but zero overlap.
    assert temporal_iou(0.0, 5.0, 5.0, 10.0) == 0.0


def test_temporal_iou_half_overlap() -> None:
    # a = [0,4], b = [2,6]. Inter=2, union=6 → 1/3.
    assert temporal_iou(0.0, 4.0, 2.0, 6.0) == pytest.approx(1.0 / 3.0)


def test_temporal_iou_subset() -> None:
    # b fully inside a. Inter=2, union=4 → 0.5.
    assert temporal_iou(0.0, 4.0, 1.0, 3.0) == pytest.approx(0.5)


def test_temporal_iou_degenerate_intervals_return_zero() -> None:
    # start >= end is degenerate; should return 0 not crash.
    assert temporal_iou(5.0, 5.0, 0.0, 10.0) == 0.0
    assert temporal_iou(0.0, 10.0, 5.0, 5.0) == 0.0


# ── spatial_iou ──────────────────────────────────────────────────────────────


_FULL_FRAME = {"x_norm": 0.5, "y_norm": 0.5, "w_norm": 1.0, "h_norm": 1.0}


def test_spatial_iou_identical_boxes() -> None:
    a = {"x_norm": 0.5, "y_norm": 0.5, "w_norm": 0.4, "h_norm": 0.2}
    assert spatial_iou(a, a) == pytest.approx(1.0)


def test_spatial_iou_disjoint_boxes_zero() -> None:
    a = {"x_norm": 0.2, "y_norm": 0.2, "w_norm": 0.1, "h_norm": 0.1}
    b = {"x_norm": 0.8, "y_norm": 0.8, "w_norm": 0.1, "h_norm": 0.1}
    assert spatial_iou(a, b) == 0.0


def test_spatial_iou_full_frame_vs_quarter() -> None:
    # quarter box inside full frame: inter = quarter area (0.5*0.5=0.25),
    # union = full frame (1.0). IoU = 0.25.
    quarter = {"x_norm": 0.25, "y_norm": 0.25, "w_norm": 0.5, "h_norm": 0.5}
    assert spatial_iou(_FULL_FRAME, quarter) == pytest.approx(0.25)


def test_spatial_iou_malformed_box_returns_zero() -> None:
    assert spatial_iou({}, {"x_norm": 0.5, "y_norm": 0.5, "w_norm": 0.1, "h_norm": 0.1}) == 0.0
    assert spatial_iou({"x_norm": "bogus"}, _FULL_FRAME) == 0.0


def test_spatial_iou_symmetry() -> None:
    a = {"x_norm": 0.5, "y_norm": 0.5, "w_norm": 0.4, "h_norm": 0.2}
    b = {"x_norm": 0.55, "y_norm": 0.5, "w_norm": 0.4, "h_norm": 0.2}
    assert spatial_iou(a, b) == pytest.approx(spatial_iou(b, a))


# ── color_delta_e ────────────────────────────────────────────────────────────


def test_color_delta_e_identical_colors_zero() -> None:
    assert color_delta_e("#FFFFFF", "#FFFFFF") == pytest.approx(0.0, abs=1e-6)


def test_color_delta_e_white_to_black_is_large() -> None:
    # Maximum perceptual distance — well above threshold.
    d = color_delta_e("#FFFFFF", "#000000")
    assert d > 50.0


def test_color_delta_e_invalid_hex_is_nan() -> None:
    assert math.isnan(color_delta_e("not-hex", "#FFFFFF"))
    assert math.isnan(color_delta_e("#FFFFFF", "abc"))
    assert math.isnan(color_delta_e("#FFF", "#FFFFFF"))  # 3-digit not supported


def test_color_delta_e_near_match_under_threshold() -> None:
    # Two near-identical whites (JPEG noise simulation).
    d = color_delta_e("#FFFFFF", "#FAFAFA")
    assert d < COLOR_MATCH_DELTA_E_THRESHOLD


def test_color_delta_e_white_vs_yellow_above_threshold() -> None:
    d = color_delta_e("#FFFFFF", "#F4D03F")
    assert d > COLOR_MATCH_DELTA_E_THRESHOLD


# ── greedy_match ─────────────────────────────────────────────────────────────


def _overlay(
    *,
    text: str = "Hello",
    x: float = 0.5,
    y: float = 0.5,
    start: float = 0.0,
    end: float = 1.0,
) -> dict:
    return {
        "sample_text": text,
        "start_s": start,
        "end_s": end,
        "bbox": {"x_norm": x, "y_norm": y, "w_norm": 0.4, "h_norm": 0.1},
        "font_color_hex": "#FFFFFF",
        "effect": "none",
        "role": "label",
    }


def test_greedy_match_empty_inputs_return_empty() -> None:
    assert greedy_match([], [_overlay()]) == []
    assert greedy_match([_overlay()], []) == []
    assert greedy_match([], []) == []


def test_greedy_match_single_pair_perfect() -> None:
    pairs = greedy_match([_overlay(text="x")], [_overlay(text="x")])
    assert len(pairs) == 1
    assert pairs[0][0] == 0 and pairs[0][1] == 0
    assert pairs[0][2] >= 0.9  # near-perfect score


def test_greedy_match_text_mismatch_blocks_pairing() -> None:
    # Same bbox/timing, completely different text — must NOT pair (text floor).
    pairs = greedy_match([_overlay(text="hello")], [_overlay(text="goodbye")])
    assert pairs == []


def test_greedy_match_prefers_higher_score_pair_first() -> None:
    # Predicted has two overlays with the same text; only one truth.
    # The bbox-closer one should win.
    pred = [
        _overlay(text="x", x=0.1),  # far from truth's x=0.5
        _overlay(text="x", x=0.5),  # matches truth's x=0.5
    ]
    truth = [_overlay(text="x", x=0.5)]
    pairs = greedy_match(pred, truth)
    assert len(pairs) == 1
    assert pairs[0][0] == 1  # second predicted (the closer one) won


def test_greedy_match_no_double_assignment() -> None:
    # Two predicted, two truth, both share text. Each predicted should pair
    # with the closest truth and no truth is matched twice.
    pred = [_overlay(text="x", x=0.2), _overlay(text="x", x=0.8)]
    truth = [_overlay(text="x", x=0.2), _overlay(text="x", x=0.8)]
    pairs = greedy_match(pred, truth)
    assert len(pairs) == 2
    used_truth = {j for _, j, _ in pairs}
    used_pred = {i for i, _, _ in pairs}
    assert used_truth == {0, 1}
    assert used_pred == {0, 1}


# ── score_overlays ───────────────────────────────────────────────────────────


def test_score_overlays_empty_truth_completeness_one() -> None:
    s = score_overlays([_overlay(text="x")], [])
    # No truth means we can't miss anything; completeness defined as 1.0.
    assert s.completeness == 1.0
    assert s.matched_count == 0
    # Precision is 0 because there ARE predictions but no truth to match.
    assert s.precision == 0.0


def test_score_overlays_empty_both_completeness_one() -> None:
    s = score_overlays([], [])
    assert s.completeness == 1.0
    assert s.precision == 1.0
    assert s.matched_count == 0


def test_score_overlays_perfect_match() -> None:
    pred = [_overlay(text="x", x=0.5, y=0.5, start=0.0, end=1.0)]
    truth = [_overlay(text="x", x=0.5, y=0.5, start=0.0, end=1.0)]
    s = score_overlays(pred, truth)
    assert s.matched_count == 1
    assert s.completeness == 1.0
    assert s.precision == 1.0
    assert s.mean_temporal_iou == pytest.approx(1.0)
    assert s.mean_spatial_iou == pytest.approx(1.0)
    assert s.color_match_fraction == 1.0
    assert s.effect_label_accuracy == 1.0


def test_score_overlays_missing_half_completeness_half() -> None:
    truth = [_overlay(text="a"), _overlay(text="b")]
    pred = [_overlay(text="a")]
    s = score_overlays(pred, truth)
    assert s.completeness == 0.5
    assert s.precision == 1.0


def test_score_overlays_extra_prediction_drags_precision() -> None:
    truth = [_overlay(text="a")]
    pred = [_overlay(text="a"), _overlay(text="b")]
    s = score_overlays(pred, truth)
    assert s.completeness == 1.0
    assert s.precision == 0.5


def test_score_overlays_disjoint_sets_zero_match() -> None:
    truth = [_overlay(text="a"), _overlay(text="b")]
    pred = [_overlay(text="c"), _overlay(text="d")]
    s = score_overlays(pred, truth)
    assert s.matched_count == 0
    assert s.completeness == 0.0
    assert s.precision == 0.0


def test_score_overlays_unparseable_colors_mean_is_nan() -> None:
    pred = [_overlay(text="a")]
    pred[0]["font_color_hex"] = "garbage"
    truth = [_overlay(text="a")]
    truth[0]["font_color_hex"] = "also-garbage"
    s = score_overlays(pred, truth)
    assert s.matched_count == 1
    assert math.isnan(s.mean_color_delta_e)
    # Judge dict converts NaN to None for clean serialization.
    assert s.to_judge_dict()["mean_color_delta_e"] is None


def test_score_overlays_effect_mismatch_affects_label_accuracy() -> None:
    pred = [_overlay(text="a")]
    pred[0]["effect"] = "pop-in"
    truth = [_overlay(text="a")]
    truth[0]["effect"] = "font-cycle"
    s = score_overlays(pred, truth)
    assert s.effect_label_accuracy == 0.0
    assert s.matched_count == 1  # the text match alone still pairs them


def test_score_overlays_to_judge_dict_contains_required_keys() -> None:
    s = score_overlays([_overlay(text="a")], [_overlay(text="a")])
    d = s.to_judge_dict()
    for k in (
        "predicted_count",
        "truth_count",
        "matched_count",
        "completeness",
        "precision",
        "mean_temporal_iou",
        "mean_spatial_iou",
        "color_match_fraction",
        "effect_label_accuracy",
        "role_label_accuracy",
    ):
        assert k in d, f"judge dict missing key {k}"
