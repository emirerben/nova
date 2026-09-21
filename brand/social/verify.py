"""Measurement helpers: does a placement survive the apps' UI, and can you read it?

Two checks back every claim in README.md:

  * `check_rect`  - geometric. Does this rectangle collide with TikTok / Reels /
    Shorts chrome, using the measured occlusion map in `kria_brand.OCCLUSION`?
  * `contrast`    - photometric. Composite the asset over a real background and
    measure WCAG contrast between the ink and the pixels immediately around it,
    tile by tile, so a mark that is legible on average but vanishes over one
    bright patch still fails.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np

from kria_brand import OCCLUSION

# WCAG 2.2 minimum for large text. This is the floor for anything a viewer has
# to READ -- hook titles, step labels, captions.
CONTRAST_FLOOR = 3.0
CONTRAST_TARGET = 4.5

# The watermark is NOT gated on contrast. It is a near-subliminal attribution
# mark, deliberately chosen at a weight where it disappears into bright footage
# rather than one that stays readable -- see README section 2. Measuring it is
# still worth doing, so every pairing is reported against this reference value
# and the trade stays visible; nothing fails because of it. What the build DOES
# gate on is geometry (the mark clears the platforms' caption block) and the
# approved weights (nobody nudges the opacity by accident).
#
# Text assets -- hook titles, step labels, captions -- are a different matter
# and are still gated at CONTRAST_FLOOR above.
WATERMARK_REFERENCE = 2.0


def _intersects(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def check_rect(rect: tuple[int, int, int, int]) -> dict[str, list[str]]:
    """Return, per platform, the names of chrome regions this rect collides with."""
    labels = ["top", "right-rail", "caption", "tab-bar"]
    out: dict[str, list[str]] = {}
    for platform, regions in OCCLUSION.items():
        out[platform] = [
            labels[i] for i, region in enumerate(regions) if _intersects(rect, region)
        ]
    return out


def is_clear(rect: tuple[int, int, int, int]) -> bool:
    return all(not hits for hits in check_rect(rect).values())


# --- photometry ---------------------------------------------------------------


def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    c = c / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def luminance(rgb: np.ndarray) -> np.ndarray:
    lin = _srgb_to_linear(rgb.astype(np.float64))
    return lin[..., 0] * 0.2126 + lin[..., 1] * 0.7152 + lin[..., 2] * 0.0722


def contrast_ratio(l1: float, l2: float) -> float:
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    out = mask.copy()
    for _ in range(radius):
        grown = out.copy()
        grown[1:, :] |= out[:-1, :]
        grown[:-1, :] |= out[1:, :]
        grown[:, 1:] |= out[:, :-1]
        grown[:, :-1] |= out[:, 1:]
        out = grown
    return out


def over(fg_rgba: np.ndarray, bg_rgb: np.ndarray) -> np.ndarray:
    """Straight alpha-over of an RGBA layer onto an opaque RGB background."""
    a = (fg_rgba[..., 3:4].astype(np.float64)) / 255.0
    return (fg_rgba[..., :3].astype(np.float64) * a
            + bg_rgb.astype(np.float64) * (1.0 - a)).astype(np.float64)


@dataclass
class ContrastResult:
    median: float
    worst_tile: float
    passes_floor: bool
    meets_target: bool

    def as_dict(self) -> dict:
        return {k: (round(v, 2) if isinstance(v, float) else v)
                for k, v in asdict(self).items()}


def contrast(ink_rgba: np.ndarray, composite: np.ndarray,
             tiles: tuple[int, int] = (8, 3)) -> ContrastResult:
    """Contrast between inked pixels and the ring of background just outside them.

    `ink_rgba` is the mark WITHOUT its shadow (so the mask is the glyph itself);
    `composite` is the finished frame WITH shadow, already flattened to RGB.
    Measuring against a dilated ring rather than the whole frame is what makes
    this meaningful over busy footage: only the pixels a viewer's eye actually
    compares the glyph against are counted.
    """
    ink = ink_rgba[..., 3] > 153  # >60% opaque
    ring = _dilate(ink, 7) & ~_dilate(ink, 1)
    lum = luminance(composite)

    def ratio(mask_a: np.ndarray, mask_b: np.ndarray) -> float | None:
        if mask_a.sum() < 24 or mask_b.sum() < 24:
            return None
        return contrast_ratio(float(np.median(lum[mask_a])),
                              float(np.median(lum[mask_b])))

    overall = ratio(ink, ring)
    if overall is None:
        raise ValueError("not enough ink to measure")

    h, w = ink.shape
    ratios: list[float] = []
    for ty in range(tiles[1]):
        for tx in range(tiles[0]):
            ys = slice(ty * h // tiles[1], (ty + 1) * h // tiles[1])
            xs = slice(tx * w // tiles[0], (tx + 1) * w // tiles[0])
            sub_ink = np.zeros_like(ink)
            sub_ink[ys, xs] = ink[ys, xs]
            sub_ring = np.zeros_like(ring)
            sub_ring[ys, xs] = ring[ys, xs]
            r = ratio(sub_ink, sub_ring)
            if r is not None:
                ratios.append(r)

    worst = min(ratios) if ratios else overall
    return ContrastResult(
        median=overall,
        worst_tile=worst,
        passes_floor=worst >= CONTRAST_FLOOR,
        meets_target=worst >= CONTRAST_TARGET,
    )
