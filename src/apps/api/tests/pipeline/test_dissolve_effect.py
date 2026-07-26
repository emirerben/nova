from __future__ import annotations

from dataclasses import replace

import numpy as np
import skia

from app.pipeline.dissolve_effect import (
    MEDIA_OVERLAY_DISSOLVE_PARAMS,
    render_dissolve_svg_displacement_image,
)


def _rect_image(width: int = 220, height: int = 220):
    surface = skia.Surfaces.MakeRasterN32Premul(width, height)
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorTRANSPARENT)
    paint = skia.Paint(Color=skia.ColorWHITE, AntiAlias=False)
    canvas.drawRect(skia.Rect.MakeXYWH(55, 70, 110, 80), paint)
    return surface.makeImageSnapshot()


def _alpha_pixels(img) -> int:
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
    assert img.readPixels(info, buf, row_bytes, 0, 0)
    arr = np.frombuffer(bytes(buf), dtype=np.uint8).reshape(height, width, 4)
    return int(np.count_nonzero(arr[..., 3]))


def test_media_svg_dissolve_keeps_visible_chunks_mid_exit():
    source = _rect_image()
    params = replace(
        MEDIA_OVERLAY_DISSOLVE_PARAMS,
        duration_s=1.0,
        max_scale_px=90,
        webkit_scale_cap_px=90,
        fade_start_progress=0.70,
        particle_breakup_start_progress=0.70,
        particle_cell_px=1,
    )

    source_alpha = _alpha_pixels(source)
    mid_alpha = max(
        _alpha_pixels(
            render_dissolve_svg_displacement_image(
                source,
                t_local_s,
                3.0,
                seed=211,
                params=params,
                cap_to_webkit=True,
            )
        )
        for t_local_s in (2.20, 2.30, 2.40, 2.50)
    )
    late = render_dissolve_svg_displacement_image(
        source,
        2.92,
        3.0,
        seed=211,
        params=params,
        cap_to_webkit=True,
    )

    assert mid_alpha > source_alpha * 0.35
    assert _alpha_pixels(late) < mid_alpha
