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
from .choreography import FULLRES_SWITCH_T, FrameState
from .effects import CardGeometry, CardTransform
from .effects import transform_for as _transform_for
from .spring import SpringFrame
from .video_cards import VideoCardAsset

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

    V2: a thin adapter over `render_choreography_frames` — converts each
    `SpringFrame` into a still-only `FrameState` (`scroll_x=virtual_scroll`,
    no focus) and each `CardAsset` into a `VideoCardAsset` with an empty
    video tier (`card_frame_count=0`, so the new renderer falls back to the
    static poster, exactly V1's only face source). Byte-identical output to
    the pre-V2 single-function implementation — see
    `render_choreography_frames`'s docstring for how it reproduces the
    progress-vs-position scroll split (`lagged_virtual_scroll`) that made
    this call site parity-proven in the first place; the carousel parity
    suite (`tests/quality/carousel_parity.py`) re-verifies this end to end.
    """
    frame_states = [FrameState(t_s=sf.t_s, scroll_x=sf.virtual_scroll) for sf in spring_frames]
    video_cards = [
        VideoCardAsset(
            index=card.index,
            card_frames_dir="",
            card_frame_count=0,
            full_frames_dir=None,
            full_frame_count=0,
            poster_path=card.image_path,
        )
        for card in cards
    ]
    return render_choreography_frames(
        effect,
        frame_states,
        video_cards,
        geo,
        out_dir,
        background_rgb,
        transform_fn=transform_fn,
    )


def lagged_frame_scroll_x(frame_states: list[FrameState], frame_idx: int) -> float:
    """Generalizes `lagged_virtual_scroll` (see that function's docstring for
    the full "why") to any `FrameState`-driven timeline: `render_choreography_
    frames` always drives a frame's view-timeline PROGRESS from the
    PRECEDING frame's `scroll_x`, whether that timeline came from replaying
    `spring.simulate` (via `render_carousel_frames`'s adapter above) or was
    authored directly by `choreography.build_timeline`/`rolling_timeline`.
    Applying the same one-frame lag uniformly means V1's parity-proven
    motion "feel" carries through unchanged into V2's new timelines too,
    rather than the two diverging in an untested way."""
    if frame_idx <= 0:
        return frame_states[0].scroll_x
    return frame_states[frame_idx - 1].scroll_x


def render_choreography_frames(
    effect: str,
    frame_states: list[FrameState],
    video_cards: list[VideoCardAsset],
    geo: CardGeometry,
    out_dir: str,
    background_rgb: tuple[int, int, int] = (10, 10, 12),
    *,
    transform_fn: TransformFn | None = None,
) -> list[str]:
    """Render one opaque 1080x1920 RGB PNG per `FrameState` into `out_dir` as
    `frame_%04d.png`. Superset of `render_carousel_frames`'s job (which now
    delegates here — see that function's docstring): adds ROLLING VIDEO card
    faces and FOCUS CHOREOGRAPHY (a card zooming to fullscreen and back).

    Per frame:
      - Base transform: `transform_fn` (defaults to `effects.transform_for`),
        driven by `lagged_frame_scroll_x` for progress and the frame's own
        `scroll_x` for layout position — same split `render_carousel_frames`
        always used (see `lagged_frame_scroll_x`'s docstring).
      - Card face: a video card (`card_frame_count > 0`) uses its JPEG frame
        at `min(frame_idx, count - 1)` (clamped — see `video_cards.
        resolve_video_card`'s docstring: a clip shorter than the requested
        window simply runs out of frames, and clamping to the last one holds
        that final frame rather than looping/erroring); a stills-only card
        (`card_frame_count == 0`) uses its static poster, exactly V1.
      - Focus: the card named by `FrameState.focus_card` (when `focus_t >
        0`) is lerped from its normal carousel quad toward the full-canvas
        rect (0,0)-(CANVAS_W,CANVAS_H) LINEARLY on `focus_t` — the easing
        curve itself lives in `choreography.build_timeline`, which is what
        actually advances `focus_t` frame to frame, so this lerp is
        deliberately plain. Past `FULLRES_SWITCH_T`, its face source swaps
        from the card tier to the full tier (`video_cards.FULL_TIER_*` —
        already cover-cropped to the canvas' own 9:16 aspect, unlike the
        card tier's 3:4 — so this is a clean reveal, not a cross-fade), and
        that full-tier playback starts counting from ITS OWN frame 0 at the
        moment focus first turned on (tracked via `focus_start_frame`, a
        function-local dict keyed by card index — "video starts playing on
        focus"). The focused card is ALWAYS drawn last (on top), skips its
        own shadow/opacity/dim (fullscreen has no shadow to cast and is
        never dimmed), and its rounded corner radius lerps
        `geo.corner_radius -> 0` as it approaches fullscreen.
      - Dim: every OTHER visible card multiplies toward black by
        `FrameState.dim` (a flat alpha overlay on its own quad — the same
        cheap quad approximation `_draw_quad`'s shadow already uses, not a
        true per-pixel `filter: brightness()`).
    """
    import skia  # noqa: PLC0415

    resolve_transform: TransformFn = transform_fn or _transform_for
    uses_position_split = resolve_transform is _transform_for

    os.makedirs(out_dir, exist_ok=True)

    poster_faces: dict[int, skia.Image] = {
        vc.index: _load_card_face(vc.poster_path, geo) for vc in video_cards
    }

    bg_r, bg_g, bg_b = background_rgb
    bg_color = skia.Color(bg_r, bg_g, bg_b, 255)
    sampling = skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear)

    card_w = max(1, round(geo.card_w))
    card_h = max(1, round(geo.card_h))

    # First render-frame index at which each card's focus_t crossed above 0
    # — drives "the full tier starts playing fresh on focus" (see docstring).
    # Stays empty for cards that never focus.
    focus_start_frame: dict[int, int] = {}

    full_canvas_corners = [
        (0.0, 0.0),
        (float(CANVAS_W), 0.0),
        (float(CANVAS_W), float(CANVAS_H)),
        (0.0, float(CANVAS_H)),
    ]

    out_paths: list[str] = []
    for frame_idx, fstate in enumerate(frame_states):
        surface = skia.Surface(CANVAS_W, CANVAS_H)
        canvas = surface.getCanvas()
        canvas.clear(bg_color)

        scroll_x = lagged_frame_scroll_x(frame_states, frame_idx)
        position_scroll_x = fstate.scroll_x

        # (sort_key, is_focus, corners, face, opacity, shadow_alpha, dim)
        entries: list[
            tuple[
                tuple[int, int, int],
                bool,
                list[tuple[float, float]],
                skia.Image,
                float,
                float,
                float,
            ]
        ] = []
        for vc in video_cards:
            i = vc.index
            if uses_position_split:
                t = resolve_transform(
                    effect, scroll_x, i, geo, CANVAS_W, position_scroll_x=position_scroll_x
                )
            else:
                t = resolve_transform(effect, scroll_x, i, geo, CANVAS_W)

            is_focus = fstate.focus_card == i and fstate.focus_t > 0.0

            if is_focus:
                # Reset the playback origin on every not-focused -> focused
                # transition (not just the first), so a card focused twice in
                # one timeline restarts its full-tier playback each time
                # instead of reusing a stale offset that clamps to a frozen
                # last frame.
                prev = frame_states[frame_idx - 1] if frame_idx > 0 else None
                was_focused = prev is not None and prev.focus_card == i and prev.focus_t > 0.0
                if not was_focused:
                    focus_start_frame[i] = frame_idx
                ft = max(0.0, min(1.0, fstate.focus_t))
                base_corners = project_card_corners(t, geo)
                corners = [
                    (a[0] + (b[0] - a[0]) * ft, a[1] + (b[1] - a[1]) * ft)
                    for a, b in zip(base_corners, full_canvas_corners, strict=True)
                ]
                radius = geo.corner_radius * (1.0 - ft)
                use_full = ft > FULLRES_SWITCH_T and vc.full_frames_dir and vc.full_frame_count > 0
                if use_full:
                    full_idx = frame_idx - focus_start_frame[i]
                    face = _load_jpeg_face(
                        _clamped_frame_path(vc.full_frames_dir, full_idx, vc.full_frame_count),
                        CANVAS_W,
                        CANVAS_H,
                        radius,
                    )
                elif vc.card_frames_dir and vc.card_frame_count > 0:
                    face = _load_jpeg_face(
                        _clamped_frame_path(vc.card_frames_dir, frame_idx, vc.card_frame_count),
                        card_w,
                        card_h,
                        radius,
                    )
                else:
                    face = poster_faces[i]
                sort_key = (1, t.z_index, i)  # focused card always paints last (on top)
                entries.append((sort_key, True, corners, face, 1.0, 0.0, 0.0))
            else:
                if t.opacity <= 0.0:
                    continue
                corners = project_card_corners(t, geo)
                if vc.card_frames_dir and vc.card_frame_count > 0:
                    face = _load_jpeg_face(
                        _clamped_frame_path(vc.card_frames_dir, frame_idx, vc.card_frame_count),
                        card_w,
                        card_h,
                        geo.corner_radius,
                    )
                else:
                    face = poster_faces[i]
                sort_key = (0, t.z_index, i)
                entries.append(
                    (sort_key, False, corners, face, t.opacity, t.shadow_alpha, fstate.dim)
                )

        entries.sort(key=lambda e: e[0])

        for _sort_key, _is_focus, corners, face, opacity, shadow_alpha, dim in entries:
            _draw_quad(
                canvas, face, corners, sampling, opacity=opacity, shadow_alpha=shadow_alpha, dim=dim
            )

        out_path = os.path.join(out_dir, f"frame_{frame_idx:04d}.png")
        _write_frame_png(surface.makeImageSnapshot(), out_path)
        out_paths.append(out_path)

    return out_paths


def _clamped_frame_path(frames_dir: str, frame_index: int, frame_count: int) -> str:
    idx = max(0, min(frame_index, frame_count - 1))
    return os.path.join(frames_dir, f"frame_{idx:04d}.jpg")


def _load_jpeg_face(path: str, w: int, h: int, radius: float) -> skia.Image:
    """Decode an already cover-cropped `w x h` JPEG frame (from
    `video_cards.resolve_video_card`) and clip it to a rounded rect of
    `radius` — the video-frame equivalent of `_load_card_face`, minus the
    cover-crop step (already baked in at extraction time) and re-run EVERY
    frame rather than cached (unlike `_load_card_face`'s one-shot poster:
    the whole point here is that the content changes every frame)."""
    import skia  # noqa: PLC0415

    data = skia.Data.MakeFromFileName(path)
    src = skia.Image.MakeFromEncoded(data) if data is not None else None
    if src is None:
        raise RuntimeError(f"render_choreography_frames: could not decode video frame {path!r}")

    surface = skia.Surfaces.MakeRasterN32Premul(w, h)
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorTRANSPARENT)
    rrect = skia.RRect.MakeRectXY(skia.Rect.MakeWH(w, h), radius, radius)
    canvas.save()
    canvas.clipRRect(rrect, skia.ClipOp.kIntersect, True)
    canvas.drawImageRect(
        src,
        skia.Rect.MakeWH(src.width(), src.height()),
        skia.Rect.MakeWH(w, h),
        skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear),
        None,
        skia.Canvas.SrcRectConstraint.kFast_SrcRectConstraint,
    )
    canvas.restore()
    return surface.makeImageSnapshot()


def _draw_quad(
    canvas: skia.Canvas,
    face: skia.Image,
    corners: list[tuple[float, float]],
    sampling: skia.SamplingOptions,
    *,
    opacity: float,
    shadow_alpha: float,
    dim: float = 0.0,
) -> None:
    """Composite `face` onto the screen-space quad `corners` (shadow, then
    the image itself via `setPolyToPoly`, then an optional flat black `dim`
    overlay on the same quad — CSS `filter: brightness(1 - dim)`
    approximated as a cheap alpha wash rather than a true per-pixel
    multiply). Shared primitive behind both `render_carousel_frames` (via
    `_draw_card`, V1's exact original behavior: `dim` always 0.0) and
    `render_choreography_frames`."""
    import skia  # noqa: PLC0415

    dst_points = [skia.Point(x, y) for x, y in corners]

    if shadow_alpha > 0.0:
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
        shadow_paint.setAlphaf(max(0.0, min(1.0, shadow_alpha)))
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
    paint.setAlphaf(max(0.0, min(1.0, opacity)))

    canvas.save()
    canvas.concat(matrix)
    canvas.drawImage(face, 0, 0, sampling, paint)
    canvas.restore()

    if dim > 0.0:
        dim_path = skia.Path()
        dim_path.moveTo(dst_points[0].x(), dst_points[0].y())
        for pt in dst_points[1:]:
            dim_path.lineTo(pt.x(), pt.y())
        dim_path.close()
        dim_paint = skia.Paint(AntiAlias=True)
        dim_paint.setColor(skia.ColorBLACK)
        dim_paint.setAlphaf(max(0.0, min(1.0, dim)))
        canvas.drawPath(dim_path, dim_paint)


def _draw_card(
    canvas: skia.Canvas,
    face: skia.Image,
    t: CardTransform,
    corners: list[tuple[float, float]],
    sampling: skia.SamplingOptions,
) -> None:
    """V1's original per-card draw call, now a thin shim over `_draw_quad`
    (`dim` always 0.0 — V1 has no focus/dim concept). Kept as its own
    function in case anything still calls it directly."""
    _draw_quad(canvas, face, corners, sampling, opacity=t.opacity, shadow_alpha=t.shadow_alpha)


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
