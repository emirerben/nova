"""Tilting a slide's photo or video (KRI-1138): geometry shared with the iPhone.

Pure arithmetic, no ffmpeg and no DB. The contract is frozen by
``tests/fixtures/slide-tilt-parity/v1.json`` and asserted by Python and Swift:

* the canvas is the slide's OUTPUT canvas in px (origin top-left, y down);
* the MEDIA is the photo's (cropped) region scaled the way the slide already frames it
  (cover for Fill, whole for Fit), centred on the canvas. For Fill that is larger than
  the canvas on one axis, so a turned photo keeps showing the pixels that rotate into view;
* ``rotation_deg`` is clockwise-positive about the canvas centre (y down, so the standard
  rotation matrix with a positive angle turns the picture clockwise on screen);
* only the media turns: text, ink, stickers and the watermark stay upright on top;
* what the media no longer covers shows the tilt's backdrop (blank/colour/blur).
"""

from __future__ import annotations

import math

from app.kria.framing import CropRect

Point = tuple[float, float]


def media_size(
    image_size: tuple[float, float],
    crop: CropRect | dict[str, float] | None,
    canvas: tuple[float, float],
    *,
    fit: bool,
) -> tuple[float, float]:
    """Size in canvas px of the (cropped) photo region as the slide frames it.

    ``fit=False`` is the cover scale (``max``), ``fit=True`` the whole-photo scale (``min``),
    the same choice ``cover_placement`` / ``fit_placement`` make.
    """
    iw, ih = image_size
    cw, ch = canvas
    cx, cy, cwid, chei = (
        (0.0, 0.0, 1.0, 1.0)
        if crop is None
        else (crop["x"], crop["y"], crop["width"], crop["height"])
    )
    region_w, region_h = iw * cwid, ih * chei
    x_scale, y_scale = cw / region_w, ch / region_h
    scale = min(x_scale, y_scale) if fit else max(x_scale, y_scale)
    return (region_w * scale, region_h * scale)


def rotated_bounds(width: float, height: float, rotation_deg: float) -> tuple[float, float]:
    """Axis-aligned bounding box of a ``width x height`` rectangle turned by ``rotation_deg``
    (ffmpeg's ``rotw`` / ``roth``)."""
    rad = math.radians(rotation_deg)
    cos, sin = abs(math.cos(rad)), abs(math.sin(rad))
    return (width * cos + height * sin, width * sin + height * cos)


def rotated_corners(
    width: float, height: float, rotation_deg: float, canvas: tuple[float, float]
) -> list[Point]:
    """Canvas-px positions of the media's top-left, top-right, bottom-right, bottom-left
    corners after a clockwise turn about the canvas centre."""
    rad = math.radians(rotation_deg)
    cos, sin = math.cos(rad), math.sin(rad)
    cx, cy = canvas[0] / 2, canvas[1] / 2
    out: list[Point] = []
    for dx, dy in (
        (-width / 2, -height / 2),
        (width / 2, -height / 2),
        (width / 2, height / 2),
        (-width / 2, height / 2),
    ):
        out.append((cx + dx * cos - dy * sin, cy + dx * sin + dy * cos))
    return out


def covers_canvas(
    width: float, height: float, rotation_deg: float, canvas: tuple[float, float]
) -> bool:
    """True when the turned media still covers every canvas pixel, so no backdrop shows."""
    rad = math.radians(rotation_deg)
    cos, sin = math.cos(rad), math.sin(rad)
    cw, ch = canvas
    eps = 1e-6
    for x, y in ((0.0, 0.0), (cw, 0.0), (cw, ch), (0.0, ch)):
        # Undo the turn: canvas corner -> the media's own frame.
        dx, dy = x - cw / 2, y - ch / 2
        mx, my = dx * cos + dy * sin, -dx * sin + dy * cos
        if abs(mx) > width / 2 + eps or abs(my) > height / 2 + eps:
            return False
    return True
