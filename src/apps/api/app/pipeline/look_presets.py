"""Deterministic source-media look presets shared by every FFmpeg render path.

The filter returned here is inserted after HDR normalization, crop, and the
recipe color hint, but before grids, captions, text, cards, and other overlays.
That ordering keeps graphics crisp while image-derived and video clips receive
the same treatment.
"""

from __future__ import annotations

from typing import Literal, cast

LookPreset = Literal["none", "stadium_diffusion"]

LOOK_PRESETS = frozenset({"none", "stadium_diffusion"})
DEFAULT_LOOK_PRESET: LookPreset = "none"


def normalize_look_preset(value: object) -> LookPreset:
    """Return a known preset, defaulting only missing/blank legacy values."""
    if value is None or value == "":
        return DEFAULT_LOOK_PRESET
    if isinstance(value, str) and value in LOOK_PRESETS:
        return cast(LookPreset, value)
    raise ValueError(f"Unknown look preset: {value!r}")


def _even(value: float, *, minimum: int = 2) -> int:
    rounded = max(minimum, int(round(value)))
    return rounded if rounded % 2 == 0 else rounded + 1


def stadium_diffusion_filter(
    *,
    width: int,
    height: int,
    label_prefix: str = "stadium",
) -> str:
    """Build the approved Broadcast Hybrid diffusion/distortion filter.

    Constants are fixed product behavior, not user controls:
    - neutral highlight bloom and softened micro-contrast
    - cool shadows / warm highlights
    - center-preserving radial edge pull
    - a restrained optical ghost with chromatic separation
    - deterministic fine grain and vignette

    Labels are caller-prefixed because single-pass assembly places one copy of
    this graph per clip inside the same ``filter_complex``.
    """
    if width <= 0 or height <= 0:
        raise ValueError("Look preset dimensions must be positive")
    if not label_prefix.replace("_", "").isalnum():
        raise ValueError("Look preset label prefix must be alphanumeric")

    down_w = _even(width / 2)
    down_h = _even(height / 2)
    # Bloom and the radial edge matte are intentionally built at half
    # resolution. Both are smooth fields, so bicubic upscaling is visually
    # equivalent while avoiding two full-canvas per-pixel passes per frame.
    bloom_sigma = max(1.0, min(down_w, down_h) * 0.012)

    zoom_specs = (
        (551 / 540, 979 / 960, 0.9),
        (565 / 540, 1004 / 960, 1.5),
        (582 / 540, 1034 / 960, 2.3),
        (602 / 540, 1070 / 960, 3.0),
    )
    zoom_filters: list[str] = []
    for index, (scale_x, scale_y, blur) in enumerate(zoom_specs, start=1):
        # Intermediate GBR planes may be odd-sized. Keeping the rounded value
        # (rather than forcing even) preserves the approved 1080x1920 geometry:
        # 551x979, 565x1004, 582x1034, 602x1070.
        zoom_w = max(down_w, int(round(down_w * scale_x)))
        zoom_h = max(down_h, int(round(down_h * scale_y)))
        zoom_filters.append(
            f"[{label_prefix}_z{index}]"
            f"scale={zoom_w}:{zoom_h}:flags=bicubic,"
            f"crop={down_w}:{down_h},"
            f"gblur=sigma={blur:.1f}"
            f"[{label_prefix}_zz{index}]"
        )

    optic_w = max(down_w, int(round(down_w * (584 / 540))))
    optic_h = max(down_h, int(round(down_h * (1038 / 960))))
    optic_x = max(0, int(round((optic_w - down_w) * 0.43)))
    optic_y = max(0, int(round((optic_h - down_h) * 0.46)))

    return ";".join(
        [
            (
                f"format=gbrp,split=3[{label_prefix}_grade_in][{label_prefix}_hi]"
                f"[{label_prefix}_white_src]"
            ),
            (
                f"[{label_prefix}_grade_in]"
                "curves=all='0/0.024 0.18/0.17 0.50/0.505 "
                "0.82/0.842 1/0.985',"
                "colorbalance=rs=-0.018:gs=0.022:bs=0.027:"
                "rh=0.028:gh=0.016:bh=-0.018,"
                "eq=contrast=0.93:saturation=0.95:gamma=1.018"
                f"[{label_prefix}_graded]"
            ),
            (
                f"[{label_prefix}_hi]scale={down_w}:{down_h}:flags=area,format=gray,"
                "lutyuv=y='if(gte(val,180),val,0)',"
                f"gblur=sigma={bloom_sigma:.3f}:steps=2,"
                f"lutyuv=y='val*0.16',"
                f"scale={width}:{height}:flags=bicubic[{label_prefix}_bloom_alpha]"
            ),
            (
                f"[{label_prefix}_white_src]"
                "lutrgb=r=255:g=255:b=255,format=rgb24"
                f"[{label_prefix}_white]"
            ),
            (f"[{label_prefix}_white][{label_prefix}_bloom_alpha]alphamerge[{label_prefix}_bloom]"),
            (
                f"[{label_prefix}_graded][{label_prefix}_bloom]"
                "overlay=shortest=1:format=auto,format=gbrp,"
                f"split=3[{label_prefix}_clean][{label_prefix}_dist_source]"
                f"[{label_prefix}_mask_source]"
            ),
            (
                f"[{label_prefix}_dist_source]"
                f"scale={down_w}:{down_h}:flags=area,"
                f"split=5[{label_prefix}_z1][{label_prefix}_z2]"
                f"[{label_prefix}_z3][{label_prefix}_z4][{label_prefix}_optic]"
            ),
            *zoom_filters,
            (
                f"[{label_prefix}_zz1][{label_prefix}_zz2]"
                f"blend=all_expr='A*0.60+B*0.40'[{label_prefix}_za]"
            ),
            (
                f"[{label_prefix}_zz3][{label_prefix}_zz4]"
                f"blend=all_expr='A*0.56+B*0.44'[{label_prefix}_zb]"
            ),
            (
                f"[{label_prefix}_za][{label_prefix}_zb]"
                f"blend=all_expr='A*0.54+B*0.46'[{label_prefix}_smear]"
            ),
            (
                f"[{label_prefix}_optic]"
                "lenscorrection=k1=-0.07:k2=0.022:i=bilinear,"
                f"scale={optic_w}:{optic_h}:flags=bicubic,"
                f"crop={down_w}:{down_h}:x={optic_x}:y={optic_y},"
                "gblur=sigma=1.2,rgbashift=rh=2:bh=-2"
                f"[{label_prefix}_opticfx]"
            ),
            (
                f"[{label_prefix}_smear][{label_prefix}_opticfx]"
                f"blend=all_expr='A*0.76+B*0.24',"
                f"scale={width}:{height}:flags=bicubic"
                f"[{label_prefix}_hybrid]"
            ),
            (
                f"[{label_prefix}_mask_source]"
                f"scale={down_w}:{down_h}:flags=area,format=gray,"
                "geq=lum='255*clip((hypot((X-W/2)/(W/2),"
                "(Y-H/2)/(H/2))-0.42)/0.46,0,1)',"
                f"scale={width}:{height}:flags=bicubic"
                f"[{label_prefix}_edge_mask]"
            ),
            (
                f"[{label_prefix}_clean][{label_prefix}_hybrid]"
                f"[{label_prefix}_edge_mask]maskedmerge,"
                "vignette=angle=PI/14:eval=init,"
                "noise=alls=4:allf=t+u:all_seed=5144"
            ),
        ]
    )


def look_preset_filter(
    value: object,
    *,
    width: int,
    height: int,
    label_prefix: str = "look",
) -> str | None:
    """Resolve and build a preset filter; neutral is an exact bypass."""
    preset = normalize_look_preset(value)
    if preset == "none":
        return None
    return stadium_diffusion_filter(
        width=width,
        height=height,
        label_prefix=label_prefix,
    )
