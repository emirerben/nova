"""Shared render helpers for the dissolve-out component effect.

The source effect is the SVG dissolve study at https://www.anirudh.info/dissolve:
coarse fractal noise is pushed through a linear component-transfer threshold,
optionally merged with fine fractal noise, then used as the displacement map
while scale follows easeOutCubic.  This module keeps those parameters named so
the browser preview, Skia text renderer, and FFmpeg media-overlay pass do not
drift into three different effects.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path

DISSOLVE_OUT = "dissolve-out"


@dataclass(frozen=True)
class DissolveParams:
    """Parameters mirrored from Anirudh Pareek's SVG dissolve playground."""

    duration_s: float = 1.0
    base_frequency: float = 0.004
    fine_frequency: float = 1.0
    max_scale_px: float = 2000.0
    webkit_scale_cap_px: float = 920.0
    coherence: float = 5.0
    fine_grain: bool = True
    fade_start_progress: float = 0.5
    transform_scale_growth: float = 0.10
    webkit_transform_scale_growth: float = 0.035
    ffmpeg_broad_weight: float = 0.92
    ffmpeg_fine_weight: float = 0.08
    ffmpeg_fine_scale: int = 24
    ffmpeg_max_scale_px: float | None = None
    ffmpeg_alpha_breakup_start_progress: float = 1.0
    ffmpeg_alpha_breakup_strength: float = 0.0
    ffmpeg_alpha_breakup_scale: int = 24
    particle_breakup_start_progress: float = 0.18
    particle_cell_px: int = 3

    @property
    def intercept(self) -> float:
        return -((self.coherence - 1.0) / 2.0)


DEFAULT_DISSOLVE_PARAMS = DissolveParams()
MEDIA_OVERLAY_DISSOLVE_PARAMS = replace(
    DEFAULT_DISSOLVE_PARAMS,
    base_frequency=0.005,
    max_scale_px=700.0,
    webkit_scale_cap_px=700.0,
    fade_start_progress=0.55,
    transform_scale_growth=0.035,
    webkit_transform_scale_growth=0.035,
    ffmpeg_broad_weight=0.72,
    ffmpeg_fine_weight=0.28,
    ffmpeg_fine_scale=18,
    ffmpeg_max_scale_px=700.0,
    ffmpeg_alpha_breakup_start_progress=0.42,
    ffmpeg_alpha_breakup_strength=0.78,
    ffmpeg_alpha_breakup_scale=28,
    particle_breakup_start_progress=0.42,
    particle_cell_px=1,
)

# Back-compat names used by existing tests/callers.
DISSOLVE_MAX_DURATION_S = DEFAULT_DISSOLVE_PARAMS.duration_s
DISSOLVE_FADE_PORTION = 1.0 - DEFAULT_DISSOLVE_PARAMS.fade_start_progress
DISSOLVE_MAX_SCALE_PX = DEFAULT_DISSOLVE_PARAMS.max_scale_px


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def ease_out_cubic(value: float) -> float:
    t = _clamp01(value)
    return 1.0 - (1.0 - t) ** 3


def dissolve_duration_s(
    component_duration_s: float, params: DissolveParams = DEFAULT_DISSOLVE_PARAMS
) -> float:
    return max(0.0, min(params.duration_s, float(component_duration_s)))


def dissolve_linear_progress_at(
    t_local_s: float,
    component_duration_s: float,
    params: DissolveParams = DEFAULT_DISSOLVE_PARAMS,
) -> float:
    duration_s = max(0.001, float(component_duration_s))
    dissolve_s = max(0.001, dissolve_duration_s(duration_s, params))
    start_s = max(0.0, duration_s - dissolve_s)
    return _clamp01((max(0.0, float(t_local_s)) - start_s) / dissolve_s)


def dissolve_progress_at(
    t_local_s: float,
    component_duration_s: float,
    params: DissolveParams = DEFAULT_DISSOLVE_PARAMS,
) -> float:
    return ease_out_cubic(dissolve_linear_progress_at(t_local_s, component_duration_s, params))


def dissolve_alpha_at_progress(
    progress: float, params: DissolveParams = DEFAULT_DISSOLVE_PARAMS
) -> float:
    p = _clamp01(progress)
    fade_start = _clamp01(params.fade_start_progress)
    if p <= fade_start:
        return 1.0
    fade_window = max(0.001, 1.0 - fade_start)
    return max(0.0, 1.0 - (p - fade_start) / fade_window)


def dissolve_scale_at_progress(
    progress: float,
    params: DissolveParams = DEFAULT_DISSOLVE_PARAMS,
    *,
    cap_to_webkit: bool = False,
) -> float:
    max_scale = (
        min(params.max_scale_px, params.webkit_scale_cap_px)
        if cap_to_webkit
        else params.max_scale_px
    )
    return max_scale * _clamp01(progress)


def dissolve_transform_scale_at_progress(
    progress: float,
    params: DissolveParams = DEFAULT_DISSOLVE_PARAMS,
    *,
    cap_to_webkit: bool = False,
) -> float:
    growth = (
        params.webkit_transform_scale_growth
        if cap_to_webkit
        else params.transform_scale_growth
    )
    return 1.0 + growth * _clamp01(progress)


def dissolve_progress_expr(
    component_duration_s: float, params: DissolveParams = DEFAULT_DISSOLVE_PARAMS
) -> str:
    """FFmpeg expression for ease-out progress over the exit window.

    The expression is evaluated on the component's local timeline. It remains 0
    while the component holds, then ramps 0→1 during the final dissolve window.
    """
    duration_s = max(0.001, float(component_duration_s))
    dissolve_s = max(0.001, dissolve_duration_s(duration_s, params))
    start_s = max(0.0, duration_s - dissolve_s)
    linear = f"min(max((T-{start_s:.4f})/{dissolve_s:.4f},0),1)"
    return f"(1-pow(1-{linear},3))"


def dissolve_fade_args(
    component_duration_s: float, params: DissolveParams = DEFAULT_DISSOLVE_PARAMS
) -> tuple[float, float]:
    """Return local (fade_start_s, fade_duration_s) for the alpha fade tail."""
    duration_s = max(0.001, float(component_duration_s))
    dissolve_s = max(0.001, dissolve_duration_s(duration_s, params))
    fade_s = max(0.001, dissolve_s * (1.0 - params.fade_start_progress))
    return max(0.0, duration_s - fade_s), fade_s


def render_dissolve_skia_image(
    source_img,
    t_local_s: float,
    component_duration_s: float,
    *,
    seed: int,
    params: DissolveParams = DEFAULT_DISSOLVE_PARAMS,
    cap_to_webkit: bool = False,
):
    """Render one dissolve frame with Skia's Perlin + displacement primitives.

    `source_img` is a full-frame transparent Skia image containing the component
    at its normal hold state. The output is another full-frame transparent Skia
    image after applying the dissolve state for `t_local_s`.
    """
    import skia  # noqa: PLC0415

    width = max(1, int(source_img.width()))
    height = max(1, int(source_img.height()))
    progress = dissolve_progress_at(t_local_s, component_duration_s, params)
    alpha = dissolve_alpha_at_progress(progress, params)
    if progress <= 0.0 and alpha >= 1.0:
        return source_img

    surface = skia.Surfaces.MakeRasterN32Premul(width, height)
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorTRANSPARENT)
    if alpha <= 0.0:
        return surface.makeImageSnapshot()

    displacement_filter = _skia_displacement_filter(
        source_img,
        width=width,
        height=height,
        scale_px=dissolve_scale_at_progress(progress, params, cap_to_webkit=cap_to_webkit),
        seed=seed,
        params=params,
    )
    paint = skia.Paint(AntiAlias=True)
    paint.setAlphaf(alpha)
    paint.setImageFilter(displacement_filter)

    transform_scale = dissolve_transform_scale_at_progress(
        progress, params, cap_to_webkit=cap_to_webkit
    )
    if transform_scale != 1.0:
        canvas.translate(width / 2.0, height / 2.0)
        canvas.scale(transform_scale, transform_scale)
        canvas.translate(-width / 2.0, -height / 2.0)
    canvas.drawPaint(paint)
    out = surface.makeImageSnapshot()
    return _apply_skia_particle_breakup(out, progress, seed=seed, params=params)


def render_dissolve_svg_displacement_image(
    source_img,
    t_local_s: float,
    component_duration_s: float,
    *,
    seed: int,
    params: DissolveParams = DEFAULT_DISSOLVE_PARAMS,
    cap_to_webkit: bool = False,
):
    """Render a media dissolve by mimicking the SVG turbulence displacement.

    The original SVG applies `feDisplacementMap` over a filter region far larger
    than `SourceGraphic`. For media cards, doing this as a vectorized inverse
    sample gives us the same important property: visible pixels can appear in
    surrounding areas because those output coordinates sample back into the
    original component through the noise field.
    """
    import numpy as np  # noqa: PLC0415

    width = int(source_img.width())
    height = int(source_img.height())
    progress = dissolve_progress_at(t_local_s, component_duration_s, params)
    alpha = dissolve_alpha_at_progress(progress, params)
    if progress <= 0.0 and alpha >= 1.0:
        return source_img

    src = _skia_image_to_rgba_array(source_img)
    alpha_mask = src[..., 3] > 0
    if src[..., 3].max(initial=0) == 0 or alpha <= 0.0:
        return _numpy_rgba_to_skia_image(np.zeros((height, width, 4), dtype=np.uint8))

    ys, xs = np.nonzero(alpha_mask)
    x_min_src = int(xs.min())
    x_max_src = int(xs.max()) + 1
    y_min_src = int(ys.min())
    y_max_src = int(ys.max()) + 1
    component_w = x_max_src - x_min_src
    component_h = y_max_src - y_min_src

    scale_px = dissolve_scale_at_progress(progress, params, cap_to_webkit=cap_to_webkit)
    # SVG filter region: x/y=-200%, width/height=500% relative to the component.
    # Intersect with the visible canvas because off-canvas output is discarded.
    x0 = max(0, int(x_min_src - component_w * 2))
    y0 = max(0, int(y_min_src - component_h * 2))
    x1 = min(width, int(x_min_src + component_w * 3))
    y1 = min(height, int(y_min_src + component_h * 3))
    # Also cover the active displacement radius for small components near edges.
    pad = int(np.ceil(scale_px)) + 2
    x0 = max(0, min(x0, x_min_src - pad))
    y0 = max(0, min(y0, y_min_src - pad))
    x1 = min(width, max(x1, x_max_src + pad))
    y1 = min(height, max(y1, y_max_src + pad))
    if x0 >= x1 or y0 >= y1:
        return _numpy_rgba_to_skia_image(np.zeros((height, width, 4), dtype=np.uint8))

    big = _skia_image_to_rgba_array(
        _skia_noise_image(width, height, params.base_frequency, seed)
    ).astype(np.float32) / 255.0
    big_r = np.clip(big[y0:y1, x0:x1, 0] * params.coherence + params.intercept, 0.0, 1.0)
    big_g = np.clip(big[y0:y1, x0:x1, 1] * params.coherence + params.intercept, 0.0, 1.0)
    yy, xx = np.indices((y1 - y0, x1 - x0), dtype=np.float32)
    global_xx = (xx + x0).astype(np.uint32)
    global_yy = (yy + y0).astype(np.uint32)
    fine_r = _stable_noise_grid(global_xx, global_yy, np.uint32(seed + 1543))
    fine_g = _stable_noise_grid(global_xx, global_yy, np.uint32(seed + 9473))
    fine_b = _stable_noise_grid(global_xx, global_yy, np.uint32(seed + 3571))
    broad_weight = float(params.ffmpeg_broad_weight)
    fine_weight = float(params.ffmpeg_fine_weight)
    norm = max(0.001, broad_weight + fine_weight)
    noise_x = np.clip((big_r * broad_weight + fine_r * fine_weight) / norm, 0.0, 1.0)
    noise_y = np.clip((big_g * broad_weight + fine_g * fine_weight) / norm, 0.0, 1.0)

    src_x = np.rint(xx + x0 + (noise_x - 0.5) * 2.0 * scale_px).astype(np.int32)
    src_y = np.rint(yy + y0 + (noise_y - 0.5) * 2.0 * scale_px).astype(np.int32)
    valid = (src_x >= 0) & (src_x < width) & (src_y >= 0) & (src_y < height)
    sampled = np.zeros((y1 - y0, x1 - x0, 4), dtype=np.uint8)
    sampled[valid] = src[src_y[valid], src_x[valid]]

    breakup = _clamp01((progress - params.particle_breakup_start_progress) / 0.30)
    if breakup > 0.0:
        keep = np.clip((fine_b - breakup * 0.82) / 0.18, 0.0, 1.0)
        sampled[..., 3] = np.clip(
            sampled[..., 3].astype(np.float32) * keep,
            0,
            255,
        ).astype(np.uint8)
    if alpha < 1.0:
        sampled[..., 3] = np.clip(sampled[..., 3].astype(np.float32) * alpha, 0, 255).astype(
            np.uint8
        )

    out = np.zeros((height, width, 4), dtype=np.uint8)
    out[y0:y1, x0:x1] = sampled
    return _numpy_rgba_to_skia_image(out)


def read_skia_png(path: str):
    """Decode a PNG into a Skia image for the shared dissolve frame renderer."""
    import skia  # noqa: PLC0415

    data = skia.Data.MakeWithoutCopy(Path(path).read_bytes())
    img = skia.Image.MakeFromEncoded(data)
    if img is None:
        raise ValueError(f"Could not decode dissolve source frame: {path}")
    return img


def write_skia_png(img, path: str) -> None:
    """Write a Skia image as RGBA PNG without depending on CanvasKit server bits."""
    from PIL import Image  # noqa: PLC0415

    arr = _skia_image_to_rgba_array(img)
    Image.fromarray(arr, "RGBA").save(path, "PNG", compress_level=3)


def _skia_image_to_rgba_array(img):
    import numpy as np  # noqa: PLC0415
    import skia  # noqa: PLC0415

    width = int(img.width())
    height = int(img.height())
    info = skia.ImageInfo.Make(
        width,
        height,
        skia.ColorType.kRGBA_8888_ColorType,
        skia.AlphaType.kUnpremul_AlphaType,
    )
    row_bytes = width * 4
    buf = bytearray(row_bytes * height)
    if not img.readPixels(info, buf, row_bytes, 0, 0):
        raise ValueError("Could not read dissolved Skia frame pixels")
    return np.frombuffer(bytes(buf), dtype=np.uint8).reshape(height, width, 4).copy()


def _stable_noise_grid(xx, yy, seed_hash):
    import numpy as np  # noqa: PLC0415

    hashed = xx * np.uint32(374761393) + yy * np.uint32(668265263) + seed_hash
    hashed = (hashed ^ (hashed >> np.uint32(13))) * np.uint32(1274126177)
    return ((hashed ^ (hashed >> np.uint32(16))) & np.uint32(0xFFFF)).astype(np.float32) / 65535.0


def _numpy_rgba_to_skia_image(arr):
    import skia  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    encoded = BytesIO()
    Image.fromarray(arr, "RGBA").save(encoded, "PNG", compress_level=3)
    return skia.Image.MakeFromEncoded(skia.Data.MakeWithoutCopy(encoded.getvalue()))


def _apply_skia_particle_breakup(img, progress: float, *, seed: int, params: DissolveParams):
    """Punch deterministic fine-grain alpha holes into Skia's smooth displacement.

    SVG displacement produces granular breakup because fine turbulence modulates
    every sampled pixel. Skia's displacement filter samples more continuously,
    especially on flat glyph fills, so this alpha mask restores the particle
    read while leaving the broad field displacement intact.
    """
    p = _clamp01(progress)
    if p <= params.particle_breakup_start_progress:
        return img

    import numpy as np  # noqa: PLC0415
    import skia  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    width = int(img.width())
    height = int(img.height())
    info = skia.ImageInfo.Make(
        width,
        height,
        skia.ColorType.kRGBA_8888_ColorType,
        skia.AlphaType.kUnpremul_AlphaType,
    )
    row_bytes = width * 4
    buf = bytearray(row_bytes * height)
    if not img.readPixels(info, buf, row_bytes, 0, 0):
        return img
    arr = np.frombuffer(bytes(buf), dtype=np.uint8).reshape(height, width, 4).copy()
    if arr[..., 3].max(initial=0) == 0:
        return img

    cell = max(1, int(params.particle_cell_px))
    grid_h = (height + cell - 1) // cell
    grid_w = (width + cell - 1) // cell
    yy, xx = np.indices((grid_h, grid_w), dtype=np.uint32)
    # Stable integer hash in [0, 1). Different cells survive independently,
    # but nearest-neighbor expansion keeps visible particle chunks.
    seed_hash = np.uint32((int(seed) * 2246822519) % 4294967295)
    hashed = xx * np.uint32(374761393) + yy * np.uint32(668265263) + seed_hash
    hashed = (hashed ^ (hashed >> np.uint32(13))) * np.uint32(1274126177)
    noise = ((hashed ^ (hashed >> np.uint32(16))) & np.uint32(0xFFFF)).astype(np.float32) / 65535.0
    noise = np.repeat(np.repeat(noise, cell, axis=0), cell, axis=1)[:height, :width]

    breakup = _clamp01((p - params.particle_breakup_start_progress) / 0.72)
    keep = np.clip((noise - breakup * 0.82) / 0.18, 0.0, 1.0)
    arr[..., 3] = np.clip(arr[..., 3].astype(np.float32) * keep, 0, 255).astype(np.uint8)

    png = Image.fromarray(arr, "RGBA")
    encoded = BytesIO()
    png.save(encoded, "PNG", compress_level=3)
    return skia.Image.MakeFromEncoded(skia.Data.MakeWithoutCopy(encoded.getvalue()))


def _skia_displacement_filter(
    source_img,
    *,
    width: int,
    height: int,
    scale_px: float,
    seed: int,
    params: DissolveParams,
):
    import skia  # noqa: PLC0415

    big_img = _skia_noise_image(
        width,
        height,
        params.base_frequency,
        seed,
    )
    big_filter = skia.ImageFilters.Image(big_img)
    transfer = skia.ColorFilters.Matrix(
        [
            params.coherence,
            0,
            0,
            0,
            params.intercept * 255.0,
            0,
            params.coherence,
            0,
            0,
            params.intercept * 255.0,
            0,
            0,
            1,
            0,
            0,
            0,
            0,
            0,
            1,
            0,
        ]
    )
    displacement = skia.ImageFilters.ColorFilter(transfer, big_filter)
    if params.fine_grain:
        fine_img = _skia_noise_image(
            width,
            height,
            params.fine_frequency,
            seed + 1543,
        )
        displacement = skia.ImageFilters.Merge(
            [displacement, skia.ImageFilters.Image(fine_img)]
        )

    color = skia.ImageFilters.Image(source_img)
    crop = skia.IRect.MakeXYWH(0, 0, width, height)
    return skia.ImageFilters.DisplacementMap(
        skia.ColorChannel.kR,
        skia.ColorChannel.kG,
        scale_px,
        displacement,
        color,
        crop,
    )


def _skia_noise_image(width: int, height: int, base_frequency: float, seed: int):
    import skia  # noqa: PLC0415

    surface = skia.Surfaces.MakeRasterN32Premul(width, height)
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorTRANSPARENT)
    shader = skia.PerlinNoiseShader.MakeFractalNoise(
        max(0.0, float(base_frequency)),
        max(0.0, float(base_frequency)),
        1,
        float(seed % 1000),
    )
    paint = skia.Paint(Shader=shader)
    canvas.drawRect(skia.Rect.MakeWH(width, height), paint)
    return surface.makeImageSnapshot()


def dissolve_filter_parts(
    source_label: str,
    out_label: str,
    *,
    prefix: str,
    component_duration_s: float,
    width: int,
    height: int,
    fps: float,
    seed: int,
    params: DissolveParams = DEFAULT_DISSOLVE_PARAMS,
    cap_to_webkit: bool = False,
) -> list[str]:
    """Return filtergraph parts that displace + fade one RGBA component stream.

    This mirrors the referenced SVG technique in FFmpeg terms: two seeded Perlin
    fields become x/y displacement maps. Their strength is animated by the same
    ease-out curve, while alpha fades over the second half of the exit window.
    """
    safe_w = max(2, int(width))
    safe_h = max(2, int(height))
    safe_fps = max(1.0, float(fps))
    progress = dissolve_progress_expr(component_duration_s, params)
    fade_start_s, fade_duration_s = dissolve_fade_args(component_duration_s, params)
    x_raw = f"{prefix}_xraw"
    y_raw = f"{prefix}_yraw"
    x_big = f"{prefix}_xbig"
    y_big = f"{prefix}_ybig"
    x_fine = f"{prefix}_xfine"
    y_fine = f"{prefix}_yfine"
    x_noise = f"{prefix}_xnoise"
    y_noise = f"{prefix}_ynoise"
    x_coord = f"{prefix}_xcoord"
    y_coord = f"{prefix}_ycoord"
    rgba = f"{prefix}_rgba"
    remapped = f"{prefix}_remap"
    intercept_luma = int(round(abs(params.intercept) * 255.0))
    broad_weight = params.ffmpeg_broad_weight
    fine_weight = params.ffmpeg_fine_weight
    max_scale = (
        min(params.max_scale_px, params.webkit_scale_cap_px)
        if cap_to_webkit
        else params.max_scale_px
    )
    if params.ffmpeg_max_scale_px is not None:
        max_scale = min(max_scale, params.ffmpeg_max_scale_px)
    max_scale_px = int(round(max_scale))
    parts = [
        (
            f"perlin=s={safe_w}x{safe_h}:r={safe_fps:.3f}:octaves=1:"
            f"xscale=4:yscale=4:tscale=0.35:random_mode=seed:seed={seed % 4294967295}"
            f"[{x_raw}]"
        ),
        (
            f"perlin=s={safe_w}x{safe_h}:r={safe_fps:.3f}:octaves=1:"
            f"xscale=4:yscale=4:tscale=0.45:random_mode=seed:seed={(seed + 7919) % 4294967295}"
            f"[{y_raw}]"
        ),
        (
            f"perlin=s={safe_w}x{safe_h}:r={safe_fps:.3f}:octaves=1:"
            f"xscale={params.ffmpeg_fine_scale}:yscale={params.ffmpeg_fine_scale}:"
            f"tscale=1.60:random_mode=seed:seed={(seed + 1543) % 4294967295}"
            f"[{x_fine}]"
        ),
        (
            f"perlin=s={safe_w}x{safe_h}:r={safe_fps:.3f}:octaves=1:"
            f"xscale={params.ffmpeg_fine_scale}:yscale={params.ffmpeg_fine_scale}:"
            f"tscale=1.80:random_mode=seed:seed={(seed + 9473) % 4294967295}"
            f"[{y_fine}]"
        ),
        (
            f"[{x_raw}]format=gray,"
            f"geq=lum='clip(lum(X,Y)*{params.coherence:g}-{intercept_luma},0,255)'"
            f"[{x_big}]"
        ),
        (
            f"[{y_raw}]format=gray,"
            f"geq=lum='clip(lum(X,Y)*{params.coherence:g}-{intercept_luma},0,255)'"
            f"[{y_big}]"
        ),
        (
            f"[{x_big}][{x_fine}]blend=all_expr='clip((A-128)*{broad_weight:.2f}+"
            f"(B-128)*{fine_weight:.2f}+128,0,255)'[{x_noise}]"
        ),
        (
            f"[{y_big}][{y_fine}]blend=all_expr='clip((A-128)*{broad_weight:.2f}+"
            f"(B-128)*{fine_weight:.2f}+128,0,255)'[{y_noise}]"
        ),
        (
            f"[{x_noise}]format=gray16le,"
            f"geq=lum='X+{max_scale_px}*((lum(X,Y)/257)-128)/128*{progress}'"
            f"[{x_coord}]"
        ),
        (
            f"[{y_noise}]format=gray16le,"
            f"geq=lum='Y+{max_scale_px}*((lum(X,Y)/257)-128)/128*{progress}'"
            f"[{y_coord}]"
        ),
        f"[{source_label}]format=rgba[{rgba}]",
        (
            f"[{rgba}][{x_coord}][{y_coord}]remap=fill=0x00000000,"
            f"fade=t=out:st={fade_start_s:.4f}:d={fade_duration_s:.4f}:alpha=1,"
            f"format=rgba[{remapped}]"
        ),
    ]
    if params.ffmpeg_alpha_breakup_strength <= 0.0:
        parts.append(f"[{remapped}]copy[{out_label}]")
        return parts

    alpha_src = f"{prefix}_alpha_src"
    color_src = f"{prefix}_color_src"
    alpha = f"{prefix}_alpha"
    mask_raw = f"{prefix}_mask_raw"
    mask = f"{prefix}_mask"
    broken_alpha = f"{prefix}_broken_alpha"
    breakup = (
        f"min(max(({progress}-{params.ffmpeg_alpha_breakup_start_progress:.4f})/"
        f"{max(0.001, 1.0 - params.ffmpeg_alpha_breakup_start_progress):.4f},0),1)"
    )
    strength = _clamp01(params.ffmpeg_alpha_breakup_strength)
    parts.extend(
        [
            f"[{remapped}]split=2[{color_src}][{alpha_src}]",
            f"[{alpha_src}]alphaextract[{alpha}]",
            (
                f"perlin=s={safe_w}x{safe_h}:r={safe_fps:.3f}:octaves=1:"
                f"xscale={params.ffmpeg_alpha_breakup_scale}:"
                f"yscale={params.ffmpeg_alpha_breakup_scale}:tscale=1.20:"
                f"random_mode=seed:seed={(seed + 3571) % 4294967295}[{mask_raw}]"
            ),
            f"[{mask_raw}]format=gray[{mask}]",
            (
                f"[{alpha}][{mask}]blend=all_expr='if(lte({breakup},0),A,"
                f"A*clip((B/255-{breakup}*{strength:.2f})/0.18,0,1))'"
                f"[{broken_alpha}]"
            ),
            f"[{color_src}]format=rgb24[{color_src}_rgb]",
            f"[{color_src}_rgb][{broken_alpha}]alphamerge,format=rgba[{out_label}]",
        ]
    )
    return parts
