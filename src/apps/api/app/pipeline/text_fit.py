"""Does the creator's own label fit the clip label bar? (KRI-826)

A creator's label words are their copy, so they are bounded by what the label bar can
draw, not by a character count. The bar is the montage clip label (Fraunces 58px in a
0.82 x 1080 box, `tests/fixtures/overlay_verify/kri804_montage_clip_label_width.json`),
laid out by the same `text_overlay_skia` wrap the cloud burn and the phone compile use,
with the same glyph-safe face swap (KRI-785) and shaping for emoji.

Kept out of ``app.schemas`` on purpose: skia is imported lazily, so the schema module
(and the structural evals CI, which has no libEGL) never needs it. Without skia the fit
falls back to the old measured bound (28 characters a line), fail closed.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from functools import lru_cache

import regex
import structlog

log = structlog.get_logger()

# The label bar may take two lines; a third would cover too much of the shot.
CREATOR_LABEL_MAX_LINES = 2
LABEL_BAR_FONT_FAMILY = "Fraunces"
LABEL_BAR_SIZE_PX = 58
LABEL_BAR_MAX_WIDTH_FRAC = 0.82
LABEL_BAR_CANVAS_WIDTH = 1080
# KRI-804's measured one-line bound, used only when skia is unavailable.
_FALLBACK_LINE_CHARS = 28


# KRI-826: hand-authored lines (`wrap_lines=False`) must stay inside the frame's safe
# area: 0.9 of the width, i.e. a 5% margin on each side. Every renderer (cloud Skia,
# Pillow/libass, the phone compile, iOS `AuthoredTextFit.safeWidth`) uses this rule.
AUTHORED_SAFE_WIDTH_FRAC = 0.9
AUTHORED_MIN_WIDTH_FRAC = 0.1
# KRI-885: an authored line may use the room up to this inset from each frame edge, so
# outline and shadow ink at the very edge is not clipped.
AUTHORED_EDGE_GUARD_FRAC = 0.01


def authored_safe_width(canvas_width: float, x_frac: float | None, anchor: str | None) -> float:
    """Widest row an authored line may draw at, given where it is anchored (KRI-826, KRI-885).

    The line is placed relative to ``x_frac`` (its centre, or its left/right edge for a
    "left"/"right" anchor), so its room is the distance from that point to the frame
    edge (inset by 1% of the width): a centred line at x=0.7 has 2 x (0.99 - 0.7) of the
    width. Measuring to the frame edge, not to the 5% safe margins, means a legacy line the
    creator placed off-centre that still sits inside the frame is never re-wrapped on a
    re-render; the room is capped at the safe width (0.9 of the canvas) and a point dragged
    onto the edge keeps a tenth of
    the width (it cannot fit there; its words wrap one a row and shrink to the floor
    instead of vanishing).
    """
    width = float(canvas_width)
    guard = width * AUTHORED_EDGE_GUARD_FRAC
    cx = width * (0.5 if x_frac is None else min(1.0, max(0.0, float(x_frac))))
    if anchor == "left":
        room = width - guard - cx
    elif anchor == "right":
        room = cx - guard
    else:
        room = 2.0 * min(cx - guard, width - guard - cx)
    return max(width * AUTHORED_MIN_WIDTH_FRAC, min(width * AUTHORED_SAFE_WIDTH_FRAC, room))


def break_wide_run(run: str, width_of: Callable[[str], float], max_width: float) -> list[str]:
    """Greedy rows of ``run`` no wider than ``max_width``, broken between clusters (KRI-886).

    For a run with no space to wrap at (emoji chain, CJK text, a URL). A cluster is
    never split (ZWJ sequence, flag, skin tone, base + combining mark). A cluster wider
    than ``max_width`` on its own stays whole on its own row, never dropped.
    """
    rows: list[str] = []
    current = ""
    for cluster in regex.findall(r"\X", run):
        if current and width_of(current + cluster) > max_width:
            rows.append(current)
            current = cluster
        else:
            current += cluster
    if current:
        rows.append(current)
    return rows or [run]


def _fallback_lines(text: str) -> int:
    return sum(max(1, math.ceil(len(line) / _FALLBACK_LINE_CHARS)) for line in text.split("\n"))


@lru_cache(maxsize=512)
def creator_label_lines(text: str) -> int:
    """How many lines the label bar draws ``text`` on."""
    try:
        from app.pipeline import text_overlay_skia as cloud  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - no skia here (evals CI, a bare dev venv)
        log.info("creator_label_fit_unavailable")
        return _fallback_lines(text)
    overlay = {
        "text": text,
        "font_family": LABEL_BAR_FONT_FAMILY,
        "text_size_px": LABEL_BAR_SIZE_PX,
        "max_width_frac": LABEL_BAR_MAX_WIDTH_FRAC,
    }
    typeface = cloud._typeface_for_overlay(overlay)
    _font, _size, lines = cloud._shrink_to_fit(
        text,
        typeface,
        LABEL_BAR_SIZE_PX,
        LABEL_BAR_CANVAS_WIDTH * LABEL_BAR_MAX_WIDTH_FRAC,
        shape_text=cloud._auto_shape(typeface, text, False),
    )
    return len(lines)


def creator_label_fits(text: str) -> bool:
    """True when the label bar draws ``text`` on at most ``CREATOR_LABEL_MAX_LINES``."""
    return creator_label_lines(text) <= CREATOR_LABEL_MAX_LINES
