"""Shared crop/framing math for slide posts (KRI-44).

A crop is a normalized, top-left-origin rectangle in the *upright* (display)
frame of the source — the same space as ``NormalizedSourceCrop`` — so a crop
chosen on the iPhone is identical to the one the cloud applies. The crop region
is cover-fitted into the destination canvas: scale ``s = max(cw/rw, ch/rh)``
and centered. ``cover_placement`` is mirrored by
``SlidePostFraming.placement`` in Swift and pinned by the parity fixture
``tests/fixtures/slide-framing-parity/v1.json``.
"""

from __future__ import annotations

from typing import Literal, TypedDict

Focus = Literal["center", "top", "bottom", "left", "right"]
FOCI: tuple[Focus, ...] = ("center", "top", "bottom", "left", "right")

MIN_ZOOM = 1.0
MAX_ZOOM = 3.0

# Canvas a landscape slide exports at (TikTok photo profile only).
LANDSCAPE_CANVAS = (1920, 1080)

_EPS = 1e-6


class CropRect(TypedDict):
    x: float
    y: float
    width: float
    height: float


def clamp_zoom(zoom: float) -> float:
    return max(MIN_ZOOM, min(MAX_ZOOM, float(zoom)))


def crop_for_focus(
    source_aspect: float,
    canvas_aspect: float,
    focus: Focus = "center",
    zoom: float = 1.0,
) -> CropRect | None:
    """The canvas-shaped crop of the source anchored by ``focus``.

    Takes the largest rectangle with the canvas's aspect that fits inside the
    source, divides it by ``zoom`` and anchors it by ``focus``. Aspects are
    width / height of the UPRIGHT source and of the destination canvas. Returns
    ``None`` when the result is the whole source (identity), so callers can
    store "no crop" rather than a full-frame rectangle.
    """
    if source_aspect <= 0 or canvas_aspect <= 0:
        raise ValueError("aspects must be positive")
    if focus not in FOCI:
        raise ValueError(f"unknown focus {focus!r}")
    zoom = clamp_zoom(zoom)
    if source_aspect > canvas_aspect:
        width, height = canvas_aspect / source_aspect, 1.0
    else:
        width, height = 1.0, source_aspect / canvas_aspect
    width, height = width / zoom, height / zoom
    if width >= 1 - _EPS and height >= 1 - _EPS:
        return None
    slack_x, slack_y = 1.0 - width, 1.0 - height
    x = {"left": 0.0, "right": slack_x}.get(focus, slack_x / 2)
    y = {"top": 0.0, "bottom": slack_y}.get(focus, slack_y / 2)
    return {
        "x": round(x, 6),
        "y": round(y, 6),
        "width": round(width, 6),
        "height": round(height, 6),
    }


def cover_placement(
    image_size: tuple[float, float],
    crop: CropRect | dict[str, float] | None,
    canvas: tuple[float, float],
) -> tuple[float, float, float, float]:
    """Where the FULL source image is drawn so ``crop`` covers ``canvas``.

    Returns ``(x, y, width, height)`` of the whole image in canvas pixels
    (top-left origin). With ``crop=None`` it is the plain centered cover fit.
    """
    iw, ih = image_size
    cw, ch = canvas
    cx, cy, cwid, chei = (
        (0.0, 0.0, 1.0, 1.0)
        if crop is None
        else (crop["x"], crop["y"], crop["width"], crop["height"])
    )
    region_w, region_h = iw * cwid, ih * chei
    scale = max(cw / region_w, ch / region_h)
    dest_w, dest_h = iw * scale, ih * scale
    dest_x = cw / 2 - (cx + cwid / 2) * dest_w
    dest_y = ch / 2 - (cy + chei / 2) * dest_h
    return (dest_x, dest_y, dest_w, dest_h)


def _num(value: float) -> str:
    return format(float(value), ".6f").rstrip("0").rstrip(".") or "0"


def ffmpeg_crop_filter(crop: CropRect | dict[str, float] | None) -> str:
    """The ``crop=`` filter for a normalized upright crop (empty when None).

    Even dimensions keep subsampled video encoders happy; offsets are floored.
    Runs on the auto-rotated frame, so the rectangle is upright space.
    """
    if crop is None:
        return ""
    return (
        f"crop=trunc(iw*{_num(crop['width'])}/2)*2:"
        f"trunc(ih*{_num(crop['height'])}/2)*2:"
        f"trunc(iw*{_num(crop['x'])}):trunc(ih*{_num(crop['y'])})"
    )
