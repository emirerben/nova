"""phone_card_layout -- Qendresa item 15: phone Talking cards her size and place.

Her Q1 videos (V4/V7, kit `_src/measure/sheets`) show screenshots and screen recordings
0.92-1.0 of the frame wide, in the TOP band (from the top edge down to just above her face)
or the BOTTOM band (just under her caption row, down to the bottom edge), never on the
caption row and never on her face. This module owns that geometry; grounding
(`phone_overlay_grounding.ground_phone_subtitled_overlays(card_layout=...)`) calls it.

Behind ``TALKING_CARD_LAYOUT_ENABLED`` + ``cardLayout`` verified + an approving app build
>= ``client_protocol.CARD_LAYOUT_PROTOCOL`` (`phone_rollout.talking_card_layout_planned_for`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.pipeline.render_geometry import NormalizedBox


@dataclass(frozen=True)
class CardLayoutContext:
    """What the Talking writer knows when it lays out cards.

    ``caption_band``: ``(top, bottom)`` canvas-height fractions covering every caption the plan
    draws (read from the plan's measured caption boxes, never a constant); ``None`` when the plan
    has none to read, in which case today's protected band (0.60 down to the bottom) is kept.
    ``text_windows``: SOURCE-second windows of the title and listed-text rows; no card shares
    time with them (text rows are placed after the cards and do not avoid them).
    """

    caption_band: tuple[float, float] | None = None
    text_windows: tuple[tuple[float, float], ...] = ()


# --- geometry (pure: no I/O, no settings) -------------------------------------------------

CANVAS_RATIO = 1080.0 / 1920.0  # canvas width / height; a card's height fraction needs it
MAX_WIDTH = 0.96
EDGE_INSET = 0.02
GAP = 0.015
MIN_ZONE_HEIGHT = 0.10
MIN_WIDTH = 0.15
_TIE_FRACTION = 0.01  # areas within 1% of each other count as a tie (top wins)
_EPS = 1e-9


@dataclass(frozen=True)
class CardPlacement:
    """A card box: centre ``x_frac``/``y_frac`` and width ``scale``, all canvas fractions."""

    x_frac: float
    y_frac: float
    scale: float
    zone: str  # "top" | "bottom"
    height: float

    @property
    def box(self) -> NormalizedBox:
        half_w = self.scale / 2
        half_h = self.height / 2
        return NormalizedBox(
            self.x_frac - half_w,
            self.y_frac - half_h,
            self.x_frac + half_w,
            self.y_frac + half_h,
        )


def card_height_fraction(width: float, aspect: float) -> float:
    """Canvas-height fraction of a card ``width`` (canvas-width fraction) wide with its own
    ``aspect`` (asset width / height) kept."""
    return width * CANVAS_RATIO / aspect


def boxes_intersect(a: NormalizedBox, b: NormalizedBox) -> bool:
    """Strict test: any positive-area overlap counts (no IoU tolerance). Touching edges do not."""
    return (
        a.left < b.right - _EPS
        and b.left < a.right - _EPS
        and (a.top < b.bottom - _EPS and b.top < a.bottom - _EPS)
    )


def place_card(aspect: float | None, blockers: Sequence[NormalizedBox]) -> CardPlacement | None:
    """The widest card that fits the TOP or BOTTOM band around every blocker, else ``None``.

    A full-width card spans x, so a blocker anywhere in x bounds the band: the top zone ends just
    above the highest blocker, the bottom zone starts just under the lowest one.
    """
    aspect = float(aspect) if aspect and float(aspect) > 0 else 1.0
    if blockers:
        zones = {
            "top": (EDGE_INSET, min(b.top for b in blockers) - GAP),
            "bottom": (max(b.bottom for b in blockers) + GAP, 1.0 - EDGE_INSET),
        }
    else:
        zones = {"top": (EDGE_INSET, 1.0 - EDGE_INSET)}
    fits: dict[str, CardPlacement] = {}
    for name, (zone_top, zone_bottom) in zones.items():
        zone_h = zone_bottom - zone_top
        if zone_h < MIN_ZONE_HEIGHT:
            continue
        width = min(MAX_WIDTH, zone_h * aspect / CANVAS_RATIO)
        if width < MIN_WIDTH:
            continue
        height = card_height_fraction(width, aspect)
        fits[name] = CardPlacement(0.5, zone_top + height / 2, width, name, height)
    if not fits:
        return None
    top, bottom = fits.get("top"), fits.get("bottom")
    if top is not None and bottom is not None:
        top_area = top.scale * top.height
        bottom_area = bottom.scale * bottom.height
        chosen = bottom if bottom_area > top_area * (1.0 + _TIE_FRACTION) else top
    else:
        chosen = top or bottom
    assert chosen is not None
    box = chosen.box
    inside = (
        box.left >= -_EPS and box.top >= -_EPS and box.right <= 1 + _EPS and box.bottom <= 1 + _EPS
    )
    if not inside or any(boxes_intersect(box, b) for b in blockers):
        return None
    return chosen


def blockers_for_card(
    start_s: float,
    end_s: float,
    *,
    face_regions: Sequence[Any],
    face_sampling: str,
    fallback_face_box: NormalizedBox,
    caption_box: NormalizedBox,
    placed: Sequence[tuple[float, float, NormalizedBox]],
) -> list[NormalizedBox]:
    """Every box a card on ``[start_s, end_s]`` must stay clear of (zero overlap).

    ``face_regions`` are objects with ``start_s``/``end_s``/``box`` (``ProtectedRegion``).
    Faces in time with the card block it; if none is, but faces were seen elsewhere, their union
    blocks ("never assume the face left"); if sampling did not run clean, or ran but saw no face
    anywhere (a Talking edit always has a speaker; a detector miss under a full-width card would
    cover the face), the fallback box does.
    ``placed`` are the ``(start_s, end_s, box)`` of cards already laid out: none may stack.
    """
    blockers: list[NormalizedBox] = []
    if face_sampling != "ok" or not face_regions:
        blockers.append(fallback_face_box)
    elif face_regions:
        in_time = [r.box for r in face_regions if r.start_s < end_s and start_s < r.end_s]
        if in_time:
            blockers.extend(in_time)
        else:
            boxes = [r.box for r in face_regions]
            blockers.append(
                NormalizedBox(
                    min(b.left for b in boxes),
                    min(b.top for b in boxes),
                    max(b.right for b in boxes),
                    max(b.bottom for b in boxes),
                )
            )
    blockers.append(caption_box)
    blockers.extend(box for s, e, box in placed if s < end_s and start_s < e)
    return blockers


def shift_past_text_windows(
    start_s: float,
    end_s: float,
    windows: Sequence[tuple[float, float]],
    *,
    min_on_screen_s: float,
) -> float | None:
    """The card's start moved to the end of every text window it overlaps (chained windows
    included); ``None`` when less than ``min_on_screen_s`` is left before ``end_s``."""
    start = float(start_s)
    for _ in range(len(windows) + 1):
        hit = [w_end for w_start, w_end in windows if start < w_end and w_start < end_s]
        if not hit:
            break
        start = max(hit)
    else:
        return None
    return start if end_s - start >= min_on_screen_s - _EPS else None
