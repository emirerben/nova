"""Color masks, bounding boxes, and basic glyph-shape features.

The pipeline burns text in a small number of known colors — white
(#FFFFFF) and maize gold (#F4D03F) for Waka Waka. Color masking is far
more reliable than OCR for "where is the text" because:

  - Compression keeps near-pure colors near-pure (ΔE drift ~3-8).
  - Backgrounds rarely contain pure-saturated RGB matching the brand
    palette.
  - Color tolerance can be tuned per color independently (white tolerance
    is tighter because clouds / sky / clothing leak in; maize tolerance
    can be wider because spontaneous maize content in b-roll is rare).

We then run OCR on the cropped mask region to answer "what text is in
that region," not the other way around — see ocr.py.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class BBox:
    """Pixel bounding box. (x_min, y_min) inclusive; (x_max, y_max) inclusive."""
    x_min: int
    y_min: int
    x_max: int
    y_max: int

    @property
    def width(self) -> int:
        return self.x_max - self.x_min + 1

    @property
    def height(self) -> int:
        return self.y_max - self.y_min + 1

    @property
    def centroid(self) -> tuple[float, float]:
        return ((self.x_min + self.x_max) / 2, (self.y_min + self.y_max) / 2)

    def to_relative(self, frame_w: int, frame_h: int) -> RelativeBBox:
        return RelativeBBox(
            x_frac=self.x_min / frame_w,
            y_frac=self.y_min / frame_h,
            w_frac=self.width / frame_w,
            h_frac=self.height / frame_h,
        )


@dataclass(frozen=True)
class RelativeBBox:
    """BBox normalized to frame dimensions. Comparable across aspect ratios."""
    x_frac: float
    y_frac: float
    w_frac: float
    h_frac: float

    @property
    def cx_frac(self) -> float:
        return self.x_frac + self.w_frac / 2

    @property
    def cy_frac(self) -> float:
        return self.y_frac + self.h_frac / 2


def mask_by_color(
    frame: np.ndarray,
    target_rgb: tuple[int, int, int],
    per_channel_tol: int = 25,
) -> np.ndarray:
    """Return a 2D boolean mask of pixels within tolerance of target_rgb.

    Uses per-channel absolute difference rather than ΔE — orders of magnitude
    faster than LAB conversion and adequate for the saturated brand colors
    the pipeline uses.
    """
    if frame.ndim != 3 or frame.shape[2] < 3:
        raise ValueError(f"expected (H,W,3) RGB array, got shape {frame.shape}")
    r = frame[..., 0].astype(np.int16)
    g = frame[..., 1].astype(np.int16)
    b = frame[..., 2].astype(np.int16)
    return (
        (np.abs(r - target_rgb[0]) <= per_channel_tol)
        & (np.abs(g - target_rgb[1]) <= per_channel_tol)
        & (np.abs(b - target_rgb[2]) <= per_channel_tol)
    )


def mask_white(frame: np.ndarray, min_luma: int = 235) -> np.ndarray:
    """Return mask of near-white pixels — all 3 channels above min_luma AND
    balanced (rules out saturated bright colors like yellow that have
    R≈G high but B low).
    """
    if frame.ndim != 3 or frame.shape[2] < 3:
        raise ValueError(f"expected (H,W,3) RGB array, got shape {frame.shape}")
    r = frame[..., 0]
    g = frame[..., 1]
    b = frame[..., 2]
    return (r >= min_luma) & (g >= min_luma) & (b >= min_luma)


def largest_blob_bbox(mask: np.ndarray, min_pixels: int = 50) -> BBox | None:
    """Return the bbox of the largest connected blob in a boolean mask.

    Uses cv2.connectedComponentsWithStats (cv2 is already a project dep).
    Falls back to a tight all-pixels bbox if cv2 import fails — that
    fallback is only correct when the mask is already band-cropped.
    """
    if not mask.any() or int(mask.sum()) < min_pixels:
        return None
    try:
        import cv2  # type: ignore
        m8 = mask.astype(np.uint8)
        n_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(m8, connectivity=8)
        if n_labels <= 1:
            return None
        # stats columns: x, y, w, h, area. Label 0 is background.
        areas = stats[1:, cv2.CC_STAT_AREA]
        largest_idx = int(np.argmax(areas)) + 1
        if int(stats[largest_idx, cv2.CC_STAT_AREA]) < min_pixels:
            return None
        x = int(stats[largest_idx, cv2.CC_STAT_LEFT])
        y = int(stats[largest_idx, cv2.CC_STAT_TOP])
        w = int(stats[largest_idx, cv2.CC_STAT_WIDTH])
        h = int(stats[largest_idx, cv2.CC_STAT_HEIGHT])
        return BBox(x_min=x, y_min=y, x_max=x + w - 1, y_max=y + h - 1)
    except ImportError:
        ys, xs = np.where(mask)
        if len(xs) == 0:
            return None
        return BBox(
            x_min=int(xs.min()), y_min=int(ys.min()),
            x_max=int(xs.max()), y_max=int(ys.max()),
        )


def all_pixels_bbox(mask: np.ndarray) -> BBox | None:
    """Tight bbox around all True pixels in mask — no connected-components.

    Cheaper than largest_blob_bbox; use when you've already region-cropped
    the mask and noise outside the bbox is impossible.
    """
    if not mask.any():
        return None
    ys, xs = np.where(mask)
    return BBox(
        x_min=int(xs.min()),
        y_min=int(ys.min()),
        x_max=int(xs.max()),
        y_max=int(ys.max()),
    )


def stroke_width_ratio(mask: np.ndarray, bbox: BBox) -> float:
    """Estimate stroke thickness ratio = mean(distance-transform) / bbox_height.

    Bold fonts score higher (~0.10-0.18); thin fonts score lower (~0.05-0.08).
    Uses cv2.distanceTransform (cv2 is already a project dep). Falls back
    to a pixel-fill ratio if cv2 import fails.
    """
    if bbox.height <= 0:
        return 0.0
    crop = mask[bbox.y_min:bbox.y_max + 1, bbox.x_min:bbox.x_max + 1]
    if not crop.any():
        return 0.0
    try:
        import cv2  # type: ignore
        # Pad with a 1-pixel background border so distanceTransform has
        # something to measure distance to — without padding, a tightly-cropped
        # mask that's fully foreground returns infinity-like garbage.
        crop_padded = np.pad(crop.astype(np.uint8) * 255, 1, mode="constant")
        dt = cv2.distanceTransform(crop_padded, distanceType=cv2.DIST_L2, maskSize=3)
        # Strip the padding before sampling; only the original text pixels matter.
        dt_inner = dt[1:-1, 1:-1]
        text_pixels = dt_inner[crop]
        if text_pixels.size == 0:
            return 0.0
        mean_dt = float(text_pixels.mean())
        return (mean_dt * 2.0) / bbox.height
    except ImportError:
        text_pixels = int(crop.sum())
        bbox_area = max(bbox.width * bbox.height, 1)
        return text_pixels / bbox_area * 0.5


def serif_score(mask: np.ndarray, bbox: BBox) -> float:
    """Rough 0..1 score: presence of serif-like horizontal protrusions.

    Sums pixel density in the top 10% and bottom 10% of each character
    column. Serif fonts have a higher top/bottom density spike at glyph
    extremes than sans fonts. Cheap heuristic — not a font classifier.

    A pure sans rendering scores ~0.15-0.30; a serif rendering scores
    ~0.35-0.55. Threshold at 0.35 for binary classification.
    """
    if bbox.height < 10 or bbox.width < 10:
        return 0.0
    crop = mask[bbox.y_min:bbox.y_max + 1, bbox.x_min:bbox.x_max + 1].astype(np.float32)
    if not crop.any():
        return 0.0
    h, w = crop.shape
    top_band = crop[: max(1, h // 10), :]
    bot_band = crop[-max(1, h // 10):, :]
    mid_band = crop[h // 4: 3 * h // 4, :]
    top_density = float(top_band.mean()) if top_band.size else 0.0
    bot_density = float(bot_band.mean()) if bot_band.size else 0.0
    mid_density = float(mid_band.mean()) if mid_band.size else 0.001
    # Sans: top+bot density ≈ mid density. Serif: top+bot has protrusions
    # making density higher than the body's average.
    ratio = (top_density + bot_density) / (2.0 * max(mid_density, 0.001))
    return float(np.clip(ratio, 0.0, 1.5)) / 1.5


def color_delta_e(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> float:
    """Cheap Euclidean ΔE in RGB space — good enough for "are these the same
    brand color" within 0-100 range. True CIE2000 ΔE would need a LAB
    conversion that isn't worth the dependency here.
    """
    return float(np.sqrt(
        (c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2
    ))


def sample_color(frame: np.ndarray, mask: np.ndarray) -> tuple[int, int, int]:
    """Mean RGB of the masked pixels. Returns (0,0,0) if mask is empty."""
    if not mask.any():
        return (0, 0, 0)
    pixels = frame[mask]
    return (
        int(pixels[..., 0].mean()),
        int(pixels[..., 1].mean()),
        int(pixels[..., 2].mean()),
    )
