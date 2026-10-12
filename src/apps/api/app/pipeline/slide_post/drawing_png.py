"""Paint a slide's freehand drawing into one canvas-sized transparent PNG (KRI-589 PR2).

The server twin of the iOS ``NativeDrawingPainter`` for the one case a slide
needs: a STATIC drawing at the identity transform (no scale, rotation, centre or
reveal). ``build.py`` overlays the PNG on the slide between the text layer and
the watermark. Strokes are painted with skia-python, the same dependency the
Skia text renderer uses.

Geometry is the frozen contract in ``tests/fixtures/slide-drawing-parity/v1.json``:
the authoring canvas (``canvas_aspect``) is aspect-fit and centred into the slide's
output canvas, a point maps to ``origin + p * fitSize``, and a stroke is
``width_frac * min(W, H)`` pixels wide. Ink never leaves the fit rectangle except
for a stroke's own width and the neon glow.

Textures are deterministic functions of (stroke seed, arc length), so two renders
of the same drawing are byte-identical (the normalized derivative is
content-addressed and reused). Pen, marker and neon follow the iOS painter closely.
**Chalk, spray and highlighter are approximate ports**: chalk uses the same
integer-noise tile through a different rasteriser, spray places the same dots with
Skia's anti-aliasing, and the highlighter is painted with the normal blend at the
stroke's opacity (iOS multiplies it against the frame, which a transparent overlay
cannot express), so a highlighter over a dark photo reads slightly lighter here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

import numpy as np
import skia
from PIL import Image

from app.agents._schemas.drawing import DrawingStroke
from app.schemas.slide_post import SlideDrawing

Canvas = tuple[int, int]

_MASK64 = 0xFFFFFFFFFFFFFFFF
_GOLDEN = 0x9E3779B97F4A7C15
_MIX1 = 0xBF58476D1CE4E5B9
_MIX2 = 0x94D049BB133111EB

# One stroke can be thousands of pixels long; the spray bucket already scales with
# length, this bounds the dots a pathological stroke may emit.
_MAX_SPRAY_DOTS_PER_STROKE = 60_000
_CHALK_TILE = 256

Point = tuple[float, float]


# ---------------------------------------------------------------- geometry


def fit_rect(canvas: Canvas, canvas_aspect: float) -> tuple[float, float, float, float]:
    """(origin_x, origin_y, width, height) of the authoring canvas inside the output canvas."""
    cw, ch = canvas
    output_aspect = cw / max(ch, 1)
    if canvas_aspect > output_aspect:
        fit_w, fit_h = float(cw), cw / canvas_aspect
    else:
        fit_w, fit_h = ch * canvas_aspect, float(ch)
    return (cw - fit_w) / 2, (ch - fit_h) / 2, fit_w, fit_h


def map_points(
    points: Sequence[Sequence[float]], canvas: Canvas, canvas_aspect: float
) -> list[Point]:
    ox, oy, fw, fh = fit_rect(canvas, canvas_aspect)
    return [(ox + p[0] * fw, oy + p[1] * fh) for p in points]


def stroke_width_px(stroke: DrawingStroke, canvas: Canvas) -> float:
    return stroke.width_frac * min(canvas)


# ---------------------------------------------------------------- deterministic noise


class _Random:
    """SplitMix64, integer-only: the same stream as Swift's ``DrawingRandom``."""

    def __init__(self, seed: int, salt: int) -> None:
        self._state = ((seed & _MASK64) * _GOLDEN & _MASK64) ^ (
            ((salt & _MASK64) + 1) * _MIX1 & _MASK64
        )

    def next(self) -> int:
        self._state = (self._state + _GOLDEN) & _MASK64
        z = self._state
        z = ((z ^ (z >> 30)) * _MIX1) & _MASK64
        z = ((z ^ (z >> 27)) * _MIX2) & _MASK64
        return z ^ (z >> 31)

    def unit(self) -> float:
        return (self.next() >> 11) / 9_007_199_254_740_992


@lru_cache(maxsize=1)
def _chalk_tile() -> skia.Image:
    """The 256x256 grain tile iOS generates in code: SplitMix noise, one wrapped 3x3 box
    pass, thresholded so about a third of the ink is speckled out."""
    random = _Random(589, 1)
    size = _CHALK_TILE
    noise = np.array([random.next() >> 56 for _ in range(size * size)], dtype=np.int64)
    noise = noise.reshape(size, size)
    total = np.zeros_like(noise)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            total += np.roll(noise, (-dy, -dx), axis=(0, 1))
    # Swift: min(255, max(0, (sum / 9 - 118) * 255 / 30)) in truncating Int arithmetic.
    value = np.clip(((total // 9 - 118) * 255) // 30, 0, 255).astype(np.uint8)
    rgba = np.stack([value, value, value, value], axis=-1)
    return skia.Image.fromarray(
        np.ascontiguousarray(rgba),
        colorType=skia.kRGBA_8888_ColorType,
        alphaType=skia.kPremul_AlphaType,
    )


def _chalk_offset(seed: int) -> tuple[float, float]:
    return float(abs(seed * 37) % _CHALK_TILE), float(abs(seed * 91 + 53) % _CHALK_TILE)


class _ArcSampler:
    def __init__(self, points: Sequence[Point]) -> None:
        self._points = points
        total = 0.0
        self._cumulative = [0.0]
        for a, b in zip(points, points[1:], strict=False):
            total += math.hypot(b[0] - a[0], b[1] - a[1])
            self._cumulative.append(total)

    @property
    def length(self) -> float:
        return self._cumulative[-1]

    def point(self, distance: float) -> Point:
        points, cumulative = self._points, self._cumulative
        if len(points) < 2 or distance <= 0:
            return points[0]
        low, high = 0, len(cumulative) - 1
        while low + 1 < high:
            middle = (low + high) // 2
            if cumulative[middle] <= distance:
                low = middle
            else:
                high = middle
        span = cumulative[high] - cumulative[low]
        ratio = 0.0 if span <= 1e-9 else min(1.0, (distance - cumulative[low]) / span)
        return (
            points[low][0] + (points[high][0] - points[low][0]) * ratio,
            points[low][1] + (points[high][1] - points[low][1]) * ratio,
        )


# ---------------------------------------------------------------- painting


def _color(hex_color: str, alpha: float) -> int:
    value = int(hex_color[1:], 16)
    return skia.Color(
        (value >> 16) & 0xFF,
        (value >> 8) & 0xFF,
        value & 0xFF,
        max(0, min(255, round(alpha * 255))),
    )


def _smooth_path(points: Sequence[Point]) -> skia.Path:
    """Midpoint-smoothed quadratic path through the points (the iOS ``smoothPath``)."""
    path = skia.Path()
    path.moveTo(*points[0])
    if len(points) == 2:
        path.lineTo(*points[1])
        return path
    for index in range(1, len(points) - 1):
        mid = (
            (points[index][0] + points[index + 1][0]) / 2,
            (points[index][1] + points[index + 1][1]) / 2,
        )
        path.quadTo(points[index][0], points[index][1], mid[0], mid[1])
    path.lineTo(*points[-1])
    return path


def _stroke_paint(color: int, width: float, cap: int, join: int) -> skia.Paint:
    paint = skia.Paint(AntiAlias=True)
    paint.setStyle(skia.Paint.kStroke_Style)
    paint.setStrokeWidth(width)
    paint.setStrokeCap(cap)
    paint.setStrokeJoin(join)
    paint.setColor(color)
    return paint


def _fill_or_stroke(
    canvas: skia.Canvas,
    path: skia.Path,
    head: Point,
    dot: bool,
    width: float,
    color: int,
    cap: int,
    join: int,
    *,
    square_dot: bool = False,
    blur_sigma: float | None = None,
) -> None:
    if dot:
        paint = skia.Paint(AntiAlias=True, Color=color)
        if blur_sigma:
            paint.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, blur_sigma))
        rect = skia.Rect.MakeXYWH(head[0] - width / 2, head[1] - width / 2, width, width)
        if square_dot:
            canvas.drawRect(rect, paint)
        else:
            canvas.drawOval(rect, paint)
        return
    paint = _stroke_paint(color, width, cap, join)
    if blur_sigma:
        paint.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, blur_sigma))
    canvas.drawPath(path, paint)


def _paint_spray(
    canvas: skia.Canvas, stroke: DrawingStroke, points: Sequence[Point], width: float
) -> None:
    sampler = _ArcSampler(points)
    reach = sampler.length
    width = max(width, 1.0)
    bucket = max(width * 0.22, 0.75, reach / 4000)
    count = min(40, max(5, int(width * 0.55)))
    radius = max(0.8, width * 0.05)
    buckets = int(reach / bucket) + 1
    if buckets * count > _MAX_SPRAY_DOTS_PER_STROKE:
        bucket = reach * count / _MAX_SPRAY_DOTS_PER_STROKE
    paint = skia.Paint(AntiAlias=True)
    index = 0
    while index * bucket <= reach:
        random = _Random(stroke.seed, index)
        for _ in range(count):
            position = (index + random.unit()) * bucket
            dx = dy = 0.0
            for _ in range(4):
                x, y = random.unit() * 2 - 1, random.unit() * 2 - 1
                if x * x + y * y <= 1:
                    dx, dy = x, y
                    break
            shade = 0.35 + 0.65 * random.unit()
            if position > reach:
                continue
            cx, cy = sampler.point(position)
            paint.setColor(_color(stroke.color, stroke.opacity * shade))
            canvas.drawCircle(cx + dx * width / 2, cy + dy * width / 2, radius, paint)
        index += 1


def _paint_stroke(canvas: skia.Canvas, stroke: DrawingStroke, points: Sequence[Point], unit: float):
    width = max(stroke.width_frac * unit, 0.5)
    path = _smooth_path(points)
    length = _ArcSampler(points).length
    dot = length <= 1e-9
    head = points[0]
    opacity = stroke.opacity
    round_cap, round_join = skia.Paint.kRound_Cap, skia.Paint.kRound_Join
    texture = stroke.texture
    if texture == "pen":
        _fill_or_stroke(
            canvas, path, head, dot, width, _color(stroke.color, opacity), round_cap, round_join
        )
    elif texture == "marker":
        # Flat tip: butt ends read as a chisel marker against the pen's round ones.
        _fill_or_stroke(
            canvas,
            path,
            head,
            dot,
            width,
            _color(stroke.color, opacity * 0.85),
            skia.Paint.kButt_Cap,
            round_join,
            square_dot=True,
        )
    elif texture == "highlighter":
        _fill_or_stroke(
            canvas,
            path,
            head,
            dot,
            width,
            _color(stroke.color, opacity),
            skia.Paint.kSquare_Cap,
            skia.Paint.kMiter_Join,
            square_dot=True,
        )
    elif texture == "neon":
        canvas.saveLayerAlpha(None, max(0, min(255, round(opacity * 255))))
        solid = _color(stroke.color, 1.0)
        # CoreGraphics' shadow blur is about twice a Gaussian sigma.
        glow = _color(stroke.color, 0.95)
        _fill_or_stroke(
            canvas,
            path,
            head,
            dot,
            width * 0.95,
            glow,
            round_cap,
            round_join,
            blur_sigma=width * 1.7 / 2,
        )
        _fill_or_stroke(canvas, path, head, dot, width * 0.95, solid, round_cap, round_join)
        _fill_or_stroke(
            canvas, path, head, dot, width * 0.4, skia.ColorWHITE, round_cap, round_join
        )
        canvas.restore()
    elif texture == "chalk":
        canvas.saveLayerAlpha(None, max(0, min(255, round(opacity * 255))))
        _fill_or_stroke(
            canvas,
            path,
            head,
            dot,
            width * 1.15,
            _color(stroke.color, 0.95),
            round_cap,
            round_join,
        )
        # Keep the ink only where the in-code grain tile has alpha. The tile is fixed in
        # canvas space (offset by the seed), so the grain never depends on the stroke.
        ox, oy = _chalk_offset(stroke.seed)
        shader = _chalk_tile().makeShader(
            skia.TileMode.kRepeat,
            skia.TileMode.kRepeat,
            skia.SamplingOptions(),
            skia.Matrix.Translate(ox, oy),
        )
        grain = skia.Paint()
        grain.setShader(shader)
        grain.setBlendMode(skia.BlendMode.kDstIn)
        canvas.drawPaint(grain)
        canvas.restore()
    else:  # spray
        _paint_spray(canvas, stroke, points, width)


def render_drawing_image(drawing: SlideDrawing, *, canvas: Canvas) -> skia.Image:
    """The drawing on a transparent canvas-sized surface."""
    width, height = canvas
    surface = skia.Surfaces.MakeRasterN32Premul(width, height)
    skia_canvas = surface.getCanvas()
    skia_canvas.clear(skia.ColorTRANSPARENT)
    unit = float(min(canvas))
    for stroke in drawing.strokes:
        points = map_points(stroke.points, canvas, drawing.canvas_aspect)
        _paint_stroke(skia_canvas, stroke, points, unit)
    return surface.makeImageSnapshot()


def render_drawing_rgba(drawing: SlideDrawing, *, canvas: Canvas) -> np.ndarray:
    """Straight-alpha RGBA bytes, shape (H, W, 4)."""
    image = render_drawing_image(drawing, canvas=canvas)
    info = skia.ImageInfo.Make(
        image.width(),
        image.height(),
        skia.kRGBA_8888_ColorType,
        skia.kUnpremul_AlphaType,
    )
    row_bytes = image.width() * 4
    buffer = bytearray(row_bytes * image.height())
    if not image.readPixels(info, buffer, row_bytes, 0, 0):
        raise RuntimeError("skia could not read the drawing pixels")
    return np.frombuffer(bytes(buffer), dtype=np.uint8).reshape(image.height(), image.width(), 4)


def render_drawing_png(drawing: SlideDrawing, png_path: str, *, canvas: Canvas) -> None:
    """Write the drawing as a canvas-sized transparent PNG at ``png_path``."""
    rgba = render_drawing_rgba(drawing, canvas=canvas)
    Path(png_path).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, "RGBA").save(png_path, "PNG", compress_level=3)
