"""Decide a text-overlay font size from video composition — no default constant.

The generative hero-intro used to render at a hardcoded `size_class="jumbo"` for
every clip, ignoring how much room the footage actually has. This module turns a
composition signal (a text-safe-zone box + a visual-density score, both produced
by the clip-analysis agent and expressed in final 9:16-crop terms) into a concrete
`text_size_px`:

    larger when the frame is calm and open, smaller when it is busy or the safe
    zone is tight — always *computed*, never a magic number.

Design:
- **Import-light.** No skia/PIL at module import (the heavy fit search lazy-imports
  `text_overlay_skia` inside `compute_overlay_size`), so this stays unit-testable
  and cheap to import from the orchestrator.
- **Size + placement candidates.** Size still comes from the safe box, and placement
  now exposes bounded candidate centers for editor/render use. The renderer's
  `_shrink_to_fit` remains the final clip-safety clamp.
- **Degraded-safe.** A missing/invalid safe zone falls back to a full-width centre
  band — still a *computed* fit, never the old `jumbo` constant.

`MIN_INTRO_PX` / `MAX_INTRO_PX` are the single source of truth for the intro size
range; the public ±size-nudge endpoint imports them to clamp user overrides so the
agent and the user agree on bounds.
"""

from __future__ import annotations

# Output canvas (mirrors text_overlay_skia.CANVAS_W/H — duplicated to keep this
# module skia-free at import time).
_CANVAS_W = 1080
_CANVAS_H = 1920

# Intro size envelope — EDITORIAL, not billboard. The fit-to-box search below
# maximizes size to fill the safe zone, so the ceiling is what actually decides
# the look: a high cap makes the intro fill the frame (covers the footage, reads
# as cheap/un-viral). This range matches the curated style-set scale the project
# already settled on ("restrained sizes ~38-88px", style-sets.json _doc) so the
# agent picks a TASTEFUL size from the footage instead of the largest that fits.
# Shared with the size-override endpoint's clamp + the public ±nudge.
MIN_INTRO_PX = 40
MAX_INTRO_PX = 80

# Busy frames get smaller text even when the safe box is large: density 0 keeps the
# full ceiling, density 10 cuts it by this fraction.
_DENSITY_MAX_REDUCTION = 0.45

# Safe-box inset + the fallback box when no safe zone is available.
_BOX_W_MARGIN = 0.94  # never let text run fully edge-to-edge of the safe box
_FALLBACK_BOX_W_FRAC = 0.90
_FALLBACK_BOX_H_FRAC = 0.50


def _box_fracs_from_safe_zone(safe_zone: object) -> tuple[float, float] | None:
    """Return (w_frac, h_frac) in [0,1] from a safe-zone dict, or None if absent
    or malformed. We only consume width/height here (size, not placement)."""
    if not isinstance(safe_zone, dict):
        return None
    try:
        w = float(safe_zone.get("w"))
        h = float(safe_zone.get("h"))
    except (TypeError, ValueError):
        return None
    if not (0.0 < w <= 1.0 and 0.0 < h <= 1.0):
        return None
    return w, h


def compute_overlay_size(
    text: str,
    *,
    font_family: str | None = None,
    safe_zone: object = None,
    visual_density: float = 5.0,
    min_px: int = MIN_INTRO_PX,
    max_px: int = MAX_INTRO_PX,
) -> int:
    """Largest font px at which `text` fits the composition's text-safe box,
    biased smaller as `visual_density` rises. Always returns a value in
    [min_px, max_px] — never a hardcoded default.

    `safe_zone` is `{"x","y","w","h"}` normalized to the final 9:16 frame (only
    w/h are used here). `visual_density` is 0 (empty) .. 10 (cluttered).
    `font_family` is a registry font name so the fit uses the real typeface that
    will render; None measures with the renderer's default face.
    """
    clean = (text or "").strip()
    if not clean:
        return min_px

    box = _box_fracs_from_safe_zone(safe_zone)
    if box is None:
        w_frac, h_frac = _FALLBACK_BOX_W_FRAC, _FALLBACK_BOX_H_FRAC
    else:
        w_frac, h_frac = box[0] * _BOX_W_MARGIN, box[1]

    box_w_px = w_frac * _CANVAS_W
    box_h_px = h_frac * _CANVAS_H

    density = max(0.0, min(10.0, float(visual_density)))
    effective_max = max(min_px, int(max_px * (1.0 - _DENSITY_MAX_REDUCTION * density / 10.0)))

    # Lazy import: the fit search + typeface cache live in the skia renderer.
    from app.pipeline.text_overlay_skia import (  # noqa: PLC0415
        _typeface_for_overlay,
        fit_text_size_px,
    )

    overlay_hint = {"font_family": font_family} if font_family else {}
    typeface = _typeface_for_overlay(overlay_hint)
    return fit_text_size_px(
        clean, typeface, box_w_px, box_h_px, max_px=effective_max, min_px=min_px
    )


def clamp_intro_px(px: object) -> int:
    """Clamp an incoming (user-supplied) px into [MIN_INTRO_PX, MAX_INTRO_PX].
    Raises ValueError on a non-numeric value so the API can 422."""
    value = int(px)  # raises ValueError/TypeError on junk — caller maps to 422
    return max(MIN_INTRO_PX, min(MAX_INTRO_PX, value))


def text_placement_candidate_from_safe_zone(
    safe_zone: object,
    *,
    visual_density: float = 5.0,
    source: str = "clip_safe_zone",
) -> dict | None:
    """Return one normalized text placement candidate from a clip safe zone.

    The safe zone is already expressed in final 9:16 frame coordinates by the clip
    analyzer. Keep the candidate conservative: centered within the zone, inset by
    the same width margin as sizing, and ranked lower as visual density rises.
    """
    if not isinstance(safe_zone, dict):
        return None
    try:
        x = float(safe_zone.get("x"))
        y = float(safe_zone.get("y"))
        w = float(safe_zone.get("w"))
        h = float(safe_zone.get("h"))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and 0.0 < w <= 1.0 and 0.0 < h <= 1.0):
        return None
    if x + w > 1.05 or y + h > 1.05:
        return None
    density = max(0.0, min(10.0, float(visual_density)))
    max_width = max(0.2, min(0.9, w * _BOX_W_MARGIN))
    return {
        "source": source,
        "x_frac": round(max(0.08, min(0.92, x + w / 2.0)), 4),
        "y_frac": round(max(0.12, min(0.88, y + h / 2.0)), 4),
        "max_width_frac": round(max_width, 4),
        "confidence": round(max(0.25, 1.0 - density / 14.0), 3),
    }
