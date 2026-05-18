"""Objective scoring helpers for template-text overlay evals.

Given a predicted list of overlays (from the live or cached agent run) and a
ground-truth list (OCR-derived, human-validated), compute per-dimension
fidelity numbers the judge consumes and the structural floor gates on.

Numbers are pure functions of the two lists — no LLM, no file IO. Greedy
bipartite matching is sufficient for the per-template overlay counts we
operate on (typically 5-30 overlays); the scoring outputs do not depend on
the matching being optimal, only stable.

Public entry point: `score_overlays(predicted, truth)`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# ── Geometry primitives ──────────────────────────────────────────────────────


def temporal_iou(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """Intersection-over-union of two time intervals, in [0, 1]."""
    if a_start >= a_end or b_start >= b_end:
        return 0.0
    inter = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    if inter <= 0.0:
        return 0.0
    union = max(a_end, b_end) - min(a_start, b_start)
    return inter / union if union > 0 else 0.0


def spatial_iou(box_a: dict[str, float], box_b: dict[str, float]) -> float:
    """IoU of two normalized bounding boxes.

    Each box is {x_norm, y_norm, w_norm, h_norm} where (x, y) is the CENTER.
    Returns 0.0 if either box is malformed.
    """
    try:
        ax, ay, aw, ah = (
            float(box_a["x_norm"]),
            float(box_a["y_norm"]),
            float(box_a["w_norm"]),
            float(box_a["h_norm"]),
        )
        bx, by, bw, bh = (
            float(box_b["x_norm"]),
            float(box_b["y_norm"]),
            float(box_b["w_norm"]),
            float(box_b["h_norm"]),
        )
    except (KeyError, TypeError, ValueError):
        return 0.0
    a_x1, a_x2 = ax - aw / 2.0, ax + aw / 2.0
    a_y1, a_y2 = ay - ah / 2.0, ay + ah / 2.0
    b_x1, b_x2 = bx - bw / 2.0, bx + bw / 2.0
    b_y1, b_y2 = by - bh / 2.0, by + bh / 2.0
    inter_w = max(0.0, min(a_x2, b_x2) - max(a_x1, b_x1))
    inter_h = max(0.0, min(a_y2, b_y2) - max(a_y1, b_y1))
    inter = inter_w * inter_h
    if inter <= 0.0:
        return 0.0
    a_area = max(0.0, (a_x2 - a_x1)) * max(0.0, (a_y2 - a_y1))
    b_area = max(0.0, (b_x2 - b_x1)) * max(0.0, (b_y2 - b_y1))
    union = a_area + b_area - inter
    return inter / union if union > 0 else 0.0


# ── Color distance ───────────────────────────────────────────────────────────
# CIE76 ΔE in Lab space. Tolerance ~10 is "barely noticeable difference" for
# the human eye — generous enough to allow JPEG/encoding noise and Gemini's
# eyeballing imprecision without rewarding obviously wrong colors.


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int] | None:
    s = (hex_str or "").strip().lstrip("#")
    if len(s) != 6:
        return None
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return None


def _rgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    # sRGB → XYZ (D65) → Lab. Reference values from CIE.
    def _srgb_to_linear(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (_srgb_to_linear(v) for v in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 1.00000
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def _f(t: float) -> float:
        return t ** (1.0 / 3.0) if t > 0.008856 else (7.787 * t + 16.0 / 116.0)

    fx, fy, fz = _f(x), _f(y), _f(z)
    return 116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)


def color_delta_e(a_hex: str, b_hex: str) -> float:
    """CIE76 ΔE distance between two hex colors. NaN if either is unparseable."""
    a_rgb = _hex_to_rgb(a_hex)
    b_rgb = _hex_to_rgb(b_hex)
    if a_rgb is None or b_rgb is None:
        return float("nan")
    al, aa, ab = _rgb_to_lab(a_rgb)
    bl, ba, bb = _rgb_to_lab(b_rgb)
    return math.sqrt((al - bl) ** 2 + (aa - ba) ** 2 + (ab - bb) ** 2)


# ── Bipartite matching (greedy) ──────────────────────────────────────────────


def _normalize_text(s: Any) -> str:
    return " ".join((s or "").split()).strip().lower() if isinstance(s, str) else ""


# Minimum text similarity required to pair two overlays. Below this floor,
# the pair is treated as "different overlays that coincidentally share
# geometry/timing" and never matched. Without this floor, two overlays at the
# same position with completely different text (e.g. "Look 1" at 8s and a
# rendered "Wait" hook at 9s both occupying the top-third center) score
# 0.6*0 + 0.4 * 0.5 * (1.0 + 0.0) = 0.20 — above the old 0.15 min_pair_score
# — and get matched as the same overlay. That conflates identity with
# position and silently inflates completeness.
_MIN_TEXT_MATCH_FOR_PAIR = 0.5


def _pair_score(predicted: dict, truth: dict, *, text_weight: float = 0.6) -> float:
    """Combined similarity score for matching one predicted to one truth.

    Text identity is the gate, geometry is the tie-breaker. Two overlays
    whose text doesn't match by at least `_MIN_TEXT_MATCH_FOR_PAIR` cannot
    pair regardless of how perfectly their bboxes line up — completely
    different text at the same position is two overlays, not a duplicate.
    Spatial + temporal IoU then break ties when the same text appears
    multiple times in a template (rare but possible: "Look 1", "Look 2"
    labels reuse same position/style at different times).
    """
    pt = _normalize_text(predicted.get("sample_text"))
    tt = _normalize_text(truth.get("sample_text"))
    text_match = 1.0 if pt and pt == tt else 0.0
    if text_match == 0.0:
        # Allow approximate match via prefix/suffix containment — Gemini
        # sometimes returns "Wait for it..." when truth is "Wait for it".
        if pt and tt and (pt in tt or tt in pt):
            text_match = 0.5
    if text_match < _MIN_TEXT_MATCH_FOR_PAIR:
        return 0.0
    spatial = spatial_iou(predicted.get("bbox") or {}, truth.get("bbox") or {})
    temporal = temporal_iou(
        float(predicted.get("start_s", 0.0) or 0.0),
        float(predicted.get("end_s", 0.0) or 0.0),
        float(truth.get("start_s", 0.0) or 0.0),
        float(truth.get("end_s", 0.0) or 0.0),
    )
    return text_weight * text_match + (1.0 - text_weight) * 0.5 * (spatial + temporal)


def greedy_match(
    predicted: list[dict],
    truth: list[dict],
    *,
    min_pair_score: float = 0.15,
) -> list[tuple[int, int, float]]:
    """Greedy bipartite matching: highest-scoring pair wins, both removed, repeat.

    Returns list of (predicted_index, truth_index, pair_score). Predicted or
    truth indices not present in the output were unmatched. Pairs below
    `min_pair_score` are not matched at all — we'd rather call an item
    "missing" than match it to a clearly different item just because it was
    the only option left.
    """
    if not predicted or not truth:
        return []
    candidates: list[tuple[float, int, int]] = []
    for i, p in enumerate(predicted):
        for j, t in enumerate(truth):
            s = _pair_score(p, t)
            if s >= min_pair_score:
                candidates.append((s, i, j))
    candidates.sort(reverse=True)  # highest score first
    used_p: set[int] = set()
    used_t: set[int] = set()
    matches: list[tuple[int, int, float]] = []
    for score, i, j in candidates:
        if i in used_p or j in used_t:
            continue
        used_p.add(i)
        used_t.add(j)
        matches.append((i, j, score))
    return matches


# ── Result containers ────────────────────────────────────────────────────────


@dataclass
class OverlayScores:
    """Numeric scores for the eval rubric and structural gate.

    `predicted_count`, `truth_count`, `matched_count` are raw counts; the
    fractions and means are convenience aggregates. All means are 0.0 when no
    pairs matched (caller should look at `matched_count` to interpret).
    """

    predicted_count: int = 0
    truth_count: int = 0
    matched_count: int = 0
    completeness: float = 0.0  # matched / truth
    precision: float = 0.0  # matched / predicted
    mean_temporal_iou: float = 0.0
    mean_spatial_iou: float = 0.0
    mean_color_delta_e: float = 0.0
    color_match_fraction: float = 0.0  # frac of pairs with delta_e < 10
    effect_label_accuracy: float = 0.0  # frac of pairs with matching effect
    role_label_accuracy: float = 0.0  # frac of pairs with matching role
    per_pair: list[dict] = field(default_factory=list)

    def to_judge_dict(self) -> dict[str, float | int]:
        """Compact serializable form for the judge prompt."""
        return {
            "predicted_count": self.predicted_count,
            "truth_count": self.truth_count,
            "matched_count": self.matched_count,
            "completeness": round(self.completeness, 3),
            "precision": round(self.precision, 3),
            "mean_temporal_iou": round(self.mean_temporal_iou, 3),
            "mean_spatial_iou": round(self.mean_spatial_iou, 3),
            "mean_color_delta_e": round(self.mean_color_delta_e, 2)
            if not math.isnan(self.mean_color_delta_e)
            else None,
            "color_match_fraction": round(self.color_match_fraction, 3),
            "effect_label_accuracy": round(self.effect_label_accuracy, 3),
            "role_label_accuracy": round(self.role_label_accuracy, 3),
        }


# Threshold below which the visible color difference is "indistinguishable"
# for our purposes. CIE76 ΔE=10 is roughly a clearly noticeable color change
# (full saturation step), but the prompt tells the agent to return DOMINANT
# pixel color and we accept JPEG noise, so 10 is the generous floor.
COLOR_MATCH_DELTA_E_THRESHOLD = 10.0


def score_overlays(predicted: list[dict], truth: list[dict]) -> OverlayScores:
    """Score the agent's predicted overlays against ground-truth overlays."""
    scores = OverlayScores(
        predicted_count=len(predicted),
        truth_count=len(truth),
    )
    if not truth:
        # No ground truth — only precision is meaningful. Set completeness to
        # 1.0 by convention (you cannot miss what does not exist).
        scores.completeness = 1.0
        scores.precision = 1.0 if not predicted else 0.0
        return scores

    matches = greedy_match(predicted, truth)
    scores.matched_count = len(matches)
    scores.completeness = len(matches) / len(truth)
    scores.precision = len(matches) / len(predicted) if predicted else 0.0

    if not matches:
        return scores

    temporal_ious: list[float] = []
    spatial_ious: list[float] = []
    color_deltas: list[float] = []
    color_matches = 0
    effect_matches = 0
    role_matches = 0
    pair_details: list[dict] = []

    for p_i, t_j, pair_score in matches:
        p = predicted[p_i]
        t = truth[t_j]
        t_iou = temporal_iou(
            float(p.get("start_s", 0.0) or 0.0),
            float(p.get("end_s", 0.0) or 0.0),
            float(t.get("start_s", 0.0) or 0.0),
            float(t.get("end_s", 0.0) or 0.0),
        )
        s_iou = spatial_iou(p.get("bbox") or {}, t.get("bbox") or {})
        c_delta = color_delta_e(p.get("font_color_hex", ""), t.get("font_color_hex", ""))
        temporal_ious.append(t_iou)
        spatial_ious.append(s_iou)
        if not math.isnan(c_delta):
            color_deltas.append(c_delta)
            if c_delta <= COLOR_MATCH_DELTA_E_THRESHOLD:
                color_matches += 1
        if p.get("effect") == t.get("effect"):
            effect_matches += 1
        if p.get("role") == t.get("role"):
            role_matches += 1
        pair_details.append(
            {
                "predicted_index": p_i,
                "truth_index": t_j,
                "predicted_text": p.get("sample_text"),
                "truth_text": t.get("sample_text"),
                "pair_score": round(pair_score, 3),
                "temporal_iou": round(t_iou, 3),
                "spatial_iou": round(s_iou, 3),
                "color_delta_e": round(c_delta, 2) if not math.isnan(c_delta) else None,
            }
        )

    scores.mean_temporal_iou = sum(temporal_ious) / len(temporal_ious)
    scores.mean_spatial_iou = sum(spatial_ious) / len(spatial_ious)
    scores.mean_color_delta_e = (
        sum(color_deltas) / len(color_deltas) if color_deltas else float("nan")
    )
    scores.color_match_fraction = color_matches / len(matches)
    scores.effect_label_accuracy = effect_matches / len(matches)
    scores.role_label_accuracy = role_matches / len(matches)
    scores.per_pair = pair_details
    return scores
