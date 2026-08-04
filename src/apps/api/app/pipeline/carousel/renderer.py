"""Skia frame renderer for carousel moments.

LAZY `import skia` inside functions only — the tests/evals CI job lacks libEGL,
so a module-level import breaks structural-only test collection. Follow the
`dissolve_effect.py` convention (`import skia  # noqa: PLC0415` inside each
function that needs it), not `text_overlay_skia.py`'s module-level import.

Geometry model
--------------
Each `CardTransform` (see `effects.py`) is turned into a screen-space quad by
replicating CSS `perspective(1200px)` on a container whose
`perspective-origin` is the viewport center (540, 960), combined with a
per-card `transform: rotateY(deg) translateZ(px) scale(s)` whose
transform-origin is the card's own (scaled) center. See `project_card_corners`
for the exact math — it's pure Python (no Skia) so it's independently
unit-testable.

The face image (a cover-cropped, rounded-rect-clipped still) is mapped onto
that projected quad with `skia.Matrix.setPolyToPoly`: the 4 corners of the
face's local pixel rect map to the 4 projected screen corners. Because 4
point-pairs generally require a projective (not merely affine) transform,
`setPolyToPoly` produces a real perspective matrix — this is the same
approximation browsers use internally for CSS 3D transforms rendered via 2D
canvas compositing, and is more than accurate enough for this effect (cards
are flat rectangles, not deep 3D meshes).
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from typing import TYPE_CHECKING

from .cards import CardAsset
from .effects import CardGeometry, CardTransform
from .effects import transform_for as _transform_for
from .spring import SpringFrame

if TYPE_CHECKING:
    import skia

FPS = 30
CANVAS_W = 1080
CANVAS_H = 1920

# -- Parity-tuning constants ---------------------------------------------------
# CSS `perspective: 1200px` on the carousel container.
PERSPECTIVE_PX = 1200.0
# CSS `perspective-origin` = viewport center for a 1080x1920 canvas.
PERSPECTIVE_ORIGIN: tuple[float, float] = (CANVAS_W / 2.0, CANVAS_H / 2.0)
# Drop shadow: vertical offset (px) and Gaussian blur sigma (px).
SHADOW_DY_PX = 18.0
SHADOW_SIGMA_PX = 24.0

# `Callable[..., CardTransform]`, not the narrower 5-positional-arg form: the
# real dispatcher (`effects.transform_for`) also takes a keyword-only
# `position_scroll_x` (see that function's docstring — layout position vs
# view-timeline progress use different, one-frame-apart scroll values), but
# the `transform_fn` escape hatch used by `test_renderer_smoke.py` doesn't
# need to accept it if it has no lag-sensitive position logic of its own.
TransformFn = Callable[..., CardTransform]


def project_card_corners(t: CardTransform, geo: CardGeometry) -> list[tuple[float, float]]:
    """Pure (no-skia) projection of one card's 4 face corners onto the
    1080x1920 canvas.

    Replicates CSS `perspective(1200px)` (perspective-origin = viewport center
    (540, 960)) combined with a per-card
    `transform: rotateY(deg) translateZ(px) scale(s)` (transform-origin = card
    center). Corner order is [top-left, top-right, bottom-right, bottom-left]
    — this MUST match the face-image source-quad order consumed by
    `skia.Matrix.setPolyToPoly` in `render_carousel_frames`.

    Model (CSS composes a `transform` list innermost-first from the RIGHT end
    of the list: `transform: A B` means B applies to the point first, then A
    applies to the result — so `rotateY(...) translateZ(...) scale(...)`
    (cover_flow's keyframes) apply scale, then translateZ, then rotateY LAST;
    `translateZ(...) rotateY(...)` (flipbook's keyframes) apply rotateY
    FIRST, then translateZ. `t.rotate_before_translate` picks which):
      - Card center on screen (pre-3D): cx = t.x + geo.card_w/2,
        cy = t.y + geo.card_h/2.
      - Corner offsets from card center after scale:
        (+/- geo.card_w/2 * t.scale, +/- geo.card_h/2 * t.scale).
      - theta = radians(t.rotate_y_deg), z0 = t.translate_z_px.
        translateZ-then-rotateY (default, `rotate_before_translate=False`):
          x1 = x0*cos(theta) + z0*sin(theta)
          z1 = -x0*sin(theta) + z0*cos(theta)
        rotateY-then-translateZ (`rotate_before_translate=True`): rotateY
        first sees z=0, so its z0-cross-terms drop out; translateZ then adds
        its raw (unrotated) z0 on top:
          x1 = x0*cos(theta)
          z1 = -x0*sin(theta) + z0
        y1 = y0 either way (rotateY doesn't touch Y).
      - Perspective projection (d = PERSPECTIVE_PX, origin = (540, 960)):
          X = (cx - 540) + x1; Y = (cy - 960) + y1; Z = z1
          f = d / (d - Z)   (denominator floored to 1.0 to guard Z >= d,
                              which would otherwise flip or blow up the sign)
          screen = (540 + X*f, 960 + Y*f)
    """
    cx = t.x + geo.card_w / 2.0
    cy = t.y + geo.card_h / 2.0
    half_w = geo.card_w / 2.0 * t.scale
    half_h = geo.card_h / 2.0 * t.scale

    theta = math.radians(t.rotate_y_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    z0 = t.translate_z_px
    origin_x, origin_y = PERSPECTIVE_ORIGIN

    corners_local = [
        (-half_w, -half_h),  # top-left
        (half_w, -half_h),  # top-right
        (half_w, half_h),  # bottom-right
        (-half_w, half_h),  # bottom-left
    ]

    projected: list[tuple[float, float]] = []
    for x0, y0 in corners_local:
        if t.rotate_before_translate:
            x1 = x0 * cos_t
            z1 = -x0 * sin_t + z0
        else:
            x1 = x0 * cos_t + z0 * sin_t
            z1 = -x0 * sin_t + z0 * cos_t
        y1 = y0

        big_x = (cx - origin_x) + x1
        big_y = (cy - origin_y) + y1
        z = z1

        # Guard Z < d: clamp the denominator away from zero/negative so a card
        # pushed past the camera plane never flips or blows up to infinity.
        denom = max(PERSPECTIVE_PX - z, 1.0)
        f = PERSPECTIVE_PX / denom

        projected.append((origin_x + big_x * f, origin_y + big_y * f))

    return projected


def lagged_virtual_scroll(spring_frames: list[SpringFrame], frame_idx: int) -> float:
    """The `virtual_scroll` value that should drive frame `frame_idx`'s VISUAL
    transform — the PREVIOUS frame's `virtual_scroll`, not this frame's own.

    Captured browser traces show the rendered `transform` (scale/rotation/
    position, everything driven by `animation-timeline: view(inline)`) lags
    one frame behind `scrollLeft` itself: `harness.js` calls `scrollTo()`
    (inside a flushed rAF callback) and then IMMEDIATELY reads
    `getComputedStyle(card).transform` in the same synchronous turn — but
    scroll-driven-animation style recalculation is a separate
    (compositor/style-invalidation) pipeline step that hasn't run yet for
    THIS turn's scroll update, so what gets read back is the state as of the
    END of the PREVIOUS frame. Verified against
    `tools/carousel_reference/out/scale_sweep/trace.json`: predicting each
    frame's `scale` from its OWN `scrollLeft` has up to 0.06 error (~6%);
    predicting it from the PRECEDING frame's `scrollLeft` matches to ~1e-6
    (floating-point noise) across every frame/card. Frame 0 (pointerdown,
    nothing has moved) has no true predecessor — it uses its own value,
    which is a no-op since nothing has changed yet anyway.
    """
    if frame_idx <= 0:
        return spring_frames[0].virtual_scroll
    return spring_frames[frame_idx - 1].virtual_scroll


def render_carousel_frames(
    effect: str,
    spring_frames: list[SpringFrame],
    cards: list[CardAsset],
    geo: CardGeometry,
    out_dir: str,
    background_rgb: tuple[int, int, int] = (10, 10, 12),
    *,
    # Deviation from spec: keyword-only escape hatch so this renderer can be
    # smoke-tested independently of `effects.transform_for` while Lane B's
    # math is still landing. Production callers never pass this — it defaults
    # to the real dispatcher.
    transform_fn: TransformFn | None = None,
) -> list[str]:
    """Render one opaque 1080x1920 RGB PNG per `SpringFrame` into `out_dir` as
    `frame_%04d.png`. Returns the list of written paths, in order.

    Per frame: fill `background_rgb`, compute each card's `CardTransform` via
    `transform_fn` (defaults to `effects.transform_for`) using
    `lagged_virtual_scroll` (NOT `spring_frame.virtual_scroll` directly — see
    that function's docstring for why), skip cards with `opacity <= 0`, sort
    the rest back-to-front by `(z_index, index)`, and composite each card's
    cached face onto its projected screen quad (with an optional blurred drop
    shadow) using `skia.Matrix.setPolyToPoly`.
    """
    import skia  # noqa: PLC0415

    resolve_transform: TransformFn = transform_fn or _transform_for
    # Only the real dispatcher (`effects.transform_for`) knows about the
    # progress-vs-layout-position scroll split (see `TransformFn`'s comment);
    # a caller-supplied `transform_fn` (the smoke-test escape hatch) gets just
    # the lagged `scroll_x`, same as before this split existed.
    uses_position_split = resolve_transform is _transform_for

    os.makedirs(out_dir, exist_ok=True)

    # Face content is static per card — load + cover-crop + rounded-rect-clip
    # ONCE per render call, outside the frame loop. Only the per-frame
    # transform changes.
    face_cache: dict[int, skia.Image] = {
        card.index: _load_card_face(card.image_path, geo) for card in cards
    }

    bg_r, bg_g, bg_b = background_rgb
    bg_color = skia.Color(bg_r, bg_g, bg_b, 255)
    sampling = skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear)

    out_paths: list[str] = []
    for frame_idx in range(len(spring_frames)):
        surface = skia.Surface(CANVAS_W, CANVAS_H)
        canvas = surface.getCanvas()
        canvas.clear(bg_color)

        scroll_x = lagged_virtual_scroll(spring_frames, frame_idx)
        position_scroll_x = spring_frames[frame_idx].virtual_scroll
        visible: list[tuple[CardTransform, CardAsset]] = []
        for card in cards:
            if uses_position_split:
                t = resolve_transform(
                    effect, scroll_x, card.index, geo, CANVAS_W, position_scroll_x=position_scroll_x
                )
            else:
                t = resolve_transform(effect, scroll_x, card.index, geo, CANVAS_W)
            if t.opacity <= 0.0:
                continue
            visible.append((t, card))

        # Back-to-front: lower z_index paints first, index breaks ties.
        visible.sort(key=lambda pair: (pair[0].z_index, pair[1].index))

        for t, card in visible:
            corners = project_card_corners(t, geo)
            _draw_card(canvas, face_cache[card.index], t, corners, sampling)

        out_path = os.path.join(out_dir, f"frame_{frame_idx:04d}.png")
        _write_frame_png(surface.makeImageSnapshot(), out_path)
        out_paths.append(out_path)

    return out_paths


def _draw_card(
    canvas: skia.Canvas,
    face: skia.Image,
    t: CardTransform,
    corners: list[tuple[float, float]],
    sampling: skia.SamplingOptions,
) -> None:
    import skia  # noqa: PLC0415

    dst_points = [skia.Point(x, y) for x, y in corners]

    if t.shadow_alpha > 0.0:
        # Approximate the shadow as the same projected quad, offset down and
        # Gaussian-blurred. A true rounded-rect shadow would need the corner
        # radius warped through the same projective matrix; the quad
        # approximation is visually indistinguishable once blurred at
        # SHADOW_SIGMA_PX and is far cheaper.
        shadow_path = skia.Path()
        shadow_path.moveTo(dst_points[0].x(), dst_points[0].y() + SHADOW_DY_PX)
        for pt in dst_points[1:]:
            shadow_path.lineTo(pt.x(), pt.y() + SHADOW_DY_PX)
        shadow_path.close()

        shadow_paint = skia.Paint(AntiAlias=True)
        shadow_paint.setColor(skia.ColorBLACK)
        shadow_paint.setAlphaf(max(0.0, min(1.0, t.shadow_alpha)))
        shadow_paint.setMaskFilter(
            skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, SHADOW_SIGMA_PX)
        )
        canvas.drawPath(shadow_path, shadow_paint)

    src_points = [
        skia.Point(0, 0),
        skia.Point(face.width(), 0),
        skia.Point(face.width(), face.height()),
        skia.Point(0, face.height()),
    ]
    matrix = skia.Matrix()
    if not matrix.setPolyToPoly(src_points, dst_points):
        return  # degenerate quad (zero area) — nothing sane to draw

    paint = skia.Paint(AntiAlias=True)
    paint.setAlphaf(max(0.0, min(1.0, t.opacity)))

    canvas.save()
    canvas.concat(matrix)
    canvas.drawImage(face, 0, 0, sampling, paint)
    canvas.restore()


def _load_card_face(image_path: str, geo: CardGeometry) -> skia.Image:
    """Decode `image_path`, cover-crop to `geo.card_w x geo.card_h` (CSS
    `object-fit: cover` semantics — center crop, no letterboxing), then clip to
    a rounded rect of `geo.corner_radius`. Returns a static RGBA image; the
    per-frame transform is applied later via `setPolyToPoly`."""
    import skia  # noqa: PLC0415

    data = skia.Data.MakeFromFileName(image_path)
    src = skia.Image.MakeFromEncoded(data) if data is not None else None
    if src is None:
        raise RuntimeError(f"render_carousel_frames: could not decode card image {image_path!r}")

    card_w = max(1, round(geo.card_w))
    card_h = max(1, round(geo.card_h))
    cropped = _cover_crop(src, card_w, card_h)

    surface = skia.Surfaces.MakeRasterN32Premul(card_w, card_h)
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorTRANSPARENT)
    rrect = skia.RRect.MakeRectXY(
        skia.Rect.MakeWH(card_w, card_h), geo.corner_radius, geo.corner_radius
    )
    canvas.save()
    canvas.clipRRect(rrect, skia.ClipOp.kIntersect, True)
    canvas.drawImage(cropped, 0, 0)
    canvas.restore()
    return surface.makeImageSnapshot()


def _cover_crop(src: skia.Image, target_w: int, target_h: int) -> skia.Image:
    """Center-crop `src` to `target_w x target_h`, matching CSS
    `object-fit: cover`: the smaller dimension is scaled to fill the target,
    and any excess on the other axis is cropped symmetrically."""
    import skia  # noqa: PLC0415

    src_w, src_h = src.width(), src.height()
    if src_w <= 0 or src_h <= 0:
        raise RuntimeError("render_carousel_frames: card image has zero dimensions")

    target_ratio = target_w / target_h
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        # Source is relatively wider than the target — crop the left/right.
        crop_h = float(src_h)
        crop_w = crop_h * target_ratio
    else:
        # Source is relatively taller than the target — crop the top/bottom.
        crop_w = float(src_w)
        crop_h = crop_w / target_ratio

    crop_x = (src_w - crop_w) / 2.0
    crop_y = (src_h - crop_h) / 2.0
    src_rect = skia.Rect.MakeXYWH(crop_x, crop_y, crop_w, crop_h)
    dst_rect = skia.Rect.MakeWH(target_w, target_h)

    surface = skia.Surfaces.MakeRasterN32Premul(target_w, target_h)
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorTRANSPARENT)
    sampling = skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear)
    canvas.drawImageRect(
        src,
        src_rect,
        dst_rect,
        sampling,
        None,
        skia.Canvas.SrcRectConstraint.kFast_SrcRectConstraint,
    )
    return surface.makeImageSnapshot()


def _write_frame_png(img: skia.Image, out_path: str) -> None:
    """Write a fully-opaque frame as an RGB PNG via Pillow, mirroring
    `text_overlay_skia._write_png_pillow`'s premultiplied-alpha handling
    (Pillow expects straight alpha, so pixels are read back as
    `kUnpremul_AlphaType`). Frames are opaque — background fill + composited
    cards leave no transparent pixels — so the alpha channel is dropped after
    the unpremultiplied read rather than written to disk."""
    import skia  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    info = skia.ImageInfo.Make(
        img.width(),
        img.height(),
        skia.ColorType.kRGBA_8888_ColorType,
        skia.AlphaType.kUnpremul_AlphaType,
    )
    row_bytes = img.width() * 4
    buf = bytearray(row_bytes * img.height())
    if not img.readPixels(info, buf, row_bytes, 0, 0):
        raise RuntimeError("render_carousel_frames: skia readPixels failed")

    pil = Image.frombytes("RGBA", (img.width(), img.height()), bytes(buf)).convert("RGB")
    pil.save(out_path, "PNG", compress_level=3)
