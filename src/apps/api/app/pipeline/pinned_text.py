"""Creator-pinned corner text (KRI-523 whole video, KRI-525 time-ranged).

One TextElement per pinned line, drawn at a named corner. A pin covers the whole video, a
seconds range, or one clip (``PinnedText.clip``, 1-based in edit order). Stacking, width
sharing and the collision rules for neighbouring text are all decided per *window*, so two
pins in the same corner at different times share a line and a label only moves out of a
bottom pin's way while that pin is actually on screen.

``y_frac`` is the block's vertical CENTRE; the safe band for platform chrome is 0.10-0.90, so
the first top line sits at 0.12 and the last bottom line at 0.86. Left/right pins are
left/right aligned at the margin; centre pins are centred.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from app.agents._schemas.text_element import TextElement
from app.schemas.edit_proposal import PinnedText

log = structlog.get_logger()

PIN_SIZE_PX = 52
PIN_MARGIN_X = 0.08
PIN_TOP_Y = 0.12
PIN_BOTTOM_Y = 0.86
PIN_LINE_STEP = math.ceil(PIN_SIZE_PX * 1.4) / 1920
# Per-clip labels and beat thoughts normally sit at 0.78-0.80; while a bottom pin is on
# screen they move up out of its way (only for snapshots that carry pins, so every other
# snapshot is unchanged).
LABEL_Y_CLEAR_OF_BOTTOM_PIN = 0.70
# A ranged pin shorter than this after clamping is not worth a layer (matches the editor's
# minimum text bar).
MIN_PIN_WINDOW_S = 0.2


@dataclass(frozen=True)
class PinWindow:
    """A pin resolved to concrete timeline seconds. ``index`` is its position on the strategy."""

    index: int
    pin: PinnedText
    start_s: float
    end_s: float

    @property
    def is_bottom(self) -> bool:
        return self.pin.corner.startswith("bottom")

    def overlaps(self, start_s: float, end_s: float) -> bool:
        return self.start_s < end_s and start_s < self.end_s


def clip_windows_from_moments(
    moments: Sequence[dict[str, Any]], total_s: float
) -> list[tuple[float, float]]:
    """Per-clip ``(start, end)`` in edit order from compiled story-timeline moments.

    A moment's own end can include the cross-fade overlap that is rendered into the next
    clip, so a clip owns the time up to the next clip's start; the last one runs to the end.
    """

    starts = [float(moment["output_start_s"]) for moment in moments]
    windows: list[tuple[float, float]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else total_s
        windows.append((start, min(end, total_s)))
    return windows


def resolve_pin_windows(
    pins: Iterable[PinnedText] | None,
    total_s: float,
    clip_windows: Sequence[tuple[float, float]] | None = None,
) -> list[PinWindow]:
    """Turn each pin's scope into timeline seconds; a pin with no usable window is dropped."""

    resolved: list[PinWindow] = []
    if total_s <= 0:
        return resolved
    for index, pin in enumerate(pins or ()):
        if pin.is_whole_video:
            resolved.append(PinWindow(index, pin, 0.0, round(total_s, 3)))
            continue
        if pin.clip is not None:
            if clip_windows is None or pin.clip > len(clip_windows):
                log.warning(
                    "pinned_text_clip_out_of_range",
                    clip=pin.clip,
                    clips=len(clip_windows or ()),
                )
                continue
            start_s, end_s = clip_windows[pin.clip - 1]
        else:
            start_s = pin.start_s or 0.0
            end_s = pin.end_s if pin.end_s is not None else total_s
        start_s = max(0.0, float(start_s))
        end_s = min(float(total_s), float(end_s))
        if end_s - start_s < MIN_PIN_WINDOW_S:
            log.warning("pinned_text_window_too_short", start_s=start_s, end_s=end_s)
            continue
        resolved.append(PinWindow(index, pin, round(start_s, 3), round(end_s, 3)))
    return resolved


def bottom_pin_overlaps(windows: Sequence[PinWindow], start_s: float, end_s: float) -> bool:
    """True when a bottom pin is on screen at any point of ``[start_s, end_s)``."""

    return any(w.is_bottom and w.overlaps(start_s, end_s) for w in windows)


# KRI-526: an opening title is centred at y 0.16, which a top pin (y 0.12, one line is ~0.038
# tall) overlaps for the whole title hold. Pins are fixed at the top of the safe band, so the
# TITLE moves down while a top pin is on screen.
TITLE_PIN_GAP = 0.012
TITLE_MAX_Y = 0.40
_TITLE_GLYPH_WIDTH = 0.55  # average advance as a fraction of the font size
_TITLE_LINE_HEIGHT = 1.15


def title_y_clear_of_pins(
    default_y: float,
    pin_edge: float | None,
    *,
    text: str,
    size_px: float,
    max_width_frac: float,
) -> float:
    """The title's centre y: ``default_y`` unless a top pin's lower edge reaches its block.

    ``pin_edge`` is the bottom edge of the lowest top pin on screen with the title (None =
    none). The block's height comes from an estimated line count (the title wraps at
    ``max_width_frac`` of the 1080 px canvas), so a two-line title is moved further.
    """

    if pin_edge is None:
        return default_y
    per_line = max(1.0, max_width_frac * 1080 / (_TITLE_GLYPH_WIDTH * size_px))
    lines = max(1, math.ceil(len(text) / per_line))
    half_block = lines * size_px * _TITLE_LINE_HEIGHT / 1920 / 2
    return round(min(TITLE_MAX_Y, max(default_y, pin_edge + TITLE_PIN_GAP + half_block)), 4)


def top_pin_edge(windows: Sequence[PinWindow], start_s: float, end_s: float) -> float | None:
    """Lower edge of the lowest top-corner pin on screen at any point of ``[start_s, end_s)``."""

    ranks = _stack_ranks(windows)
    edges = [
        PIN_TOP_Y + ranks[w.index] * PIN_LINE_STEP + PIN_LINE_STEP / 2
        for w in windows
        if not w.is_bottom and w.overlaps(start_s, end_s)
    ]
    return max(edges) if edges else None


def top_pin_edge_from_rows(rows: Sequence[dict], start_s: float, end_s: float) -> float | None:
    """`top_pin_edge` for already-compiled text rows (`guided-pinned-*`), used by face placement."""

    edges = [
        float(row["y_frac"]) + PIN_LINE_STEP / 2
        for row in rows
        if str(row.get("id", "")).startswith("guided-pinned-")
        and row.get("y_frac") is not None
        and float(row["y_frac"]) < 0.5
        and float(row.get("start_s", 0.0)) < end_s
        and start_s < float(row.get("end_s", end_s))
    ]
    return max(edges) if edges else None


def _stack_ranks(windows: Sequence[PinWindow]) -> dict[int, int]:
    """Line slot per pin: the lowest slot no overlapping pin in the same corner already holds.

    Top corners fill downward in list order; bottom corners fill upward starting from the
    LAST listed line (the last bottom line is the lowest one). For whole-video pins this is
    exactly the original rank-in-corner stacking.
    """

    ranks: dict[int, int] = {}
    by_corner: dict[str, list[PinWindow]] = {}
    for window in windows:
        by_corner.setdefault(window.pin.corner, []).append(window)
    for corner, members in by_corner.items():
        ordered = members if corner.startswith("top") else list(reversed(members))
        placed: list[PinWindow] = []
        for window in ordered:
            taken = {
                ranks[other.index]
                for other in placed
                if other.overlaps(window.start_s, window.end_s)
            }
            rank = 0
            while rank in taken:
                rank += 1
            ranks[window.index] = rank
            placed.append(window)
    return ranks


def pinned_text_elements(
    windows: Sequence[PinWindow], *, font_family: str | None, text_color: str | None
) -> list[dict]:
    if not windows:
        return []
    ranks = _stack_ranks(windows)
    elements: list[dict] = []
    for window in windows:
        pin = window.pin
        zone, side = pin.corner.split("_", 1)
        rank = ranks[window.index]
        y_frac = (
            PIN_TOP_Y + rank * PIN_LINE_STEP
            if zone == "top"
            else PIN_BOTTOM_Y - rank * PIN_LINE_STEP
        )
        # A pin must stay on ONE line (the stack step assumes it), so it gets the full width
        # between the margins; two corners sharing a vertical zone while both on screen
        # split it instead.
        shared = any(
            other.index != window.index
            and other.pin.corner.split("_", 1)[0] == zone
            and other.pin.corner.split("_", 1)[1] != side
            and other.overlaps(window.start_s, window.end_s)
            for other in windows
        )
        x_frac = {"left": PIN_MARGIN_X, "right": 1 - PIN_MARGIN_X}.get(side, 0.5)
        elements.append(
            TextElement(
                id=f"guided-pinned-{window.index}",
                text=pin.text,
                start_s=window.start_s,
                end_s=window.end_s,
                role="generative_intro",
                position="custom",
                x_frac=x_frac,
                y_frac=round(y_frac, 4),
                font_family=font_family or "Fraunces",
                size_px=PIN_SIZE_PX,
                color=text_color or "#FFF8F0",
                highlight_color="#D9FF70",
                stroke_width=0,
                shadow_enabled=True,
                shadow_style="standard",
                effect="static",
                alignment=side if side in ("left", "right") else "center",
                max_width_frac=0.42 if shared else 1 - 2 * PIN_MARGIN_X,
            ).model_dump(mode="json", exclude_none=True)
        )
    return elements
