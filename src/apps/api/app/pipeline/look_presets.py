"""Deterministic source-media look presets shared by every FFmpeg render path.

The filter returned here is inserted after HDR normalization, crop, and the
recipe color hint, but before grids, captions, text, cards, and other overlays.
That ordering keeps graphics crisp while image-derived and video clips receive
the same treatment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, assert_never, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

LookPreset = Literal[
    "none",
    "stadium_diffusion",
    "olive_film",
    "smoky_split_tone",
    "golden_hour",
    "faded_analog",
]

LOOK_PRESETS = frozenset(
    {
        "none",
        "stadium_diffusion",
        "olive_film",
        "smoky_split_tone",
        "golden_hour",
        "faded_analog",
    }
)
DEFAULT_LOOK_PRESET: LookPreset = "none"
EDIT_WIDE_LOOK_PRESETS: tuple[LookPreset, ...] = (
    "none",
    "golden_hour",
    "faded_analog",
)
FADED_VIGNETTE_MASK_PATH = (
    Path(__file__).resolve().parents[2] / "assets/looks/faded-vignette-mask.png"
).as_posix()


class LookAdjustments(BaseModel):
    """User-tunable controls for the two cinematic source looks.

    Strength scales the authored grade. Warmth and contrast are signed trims
    around that grade; grain and vignette are absolute amounts so either can
    be removed without weakening the color treatment.
    """

    intensity: float = Field(ge=0.0, le=1.0)
    warmth: float = Field(ge=-1.0, le=1.0)
    contrast: float = Field(ge=-1.0, le=1.0)
    grain: float = Field(ge=0.0, le=1.0)
    vignette: float = Field(ge=0.0, le=1.0)

    model_config = ConfigDict(extra="forbid")


_LOOK_DEFAULTS: dict[LookPreset, LookAdjustments] = {
    "olive_film": LookAdjustments(
        intensity=1.0,
        warmth=0.0,
        contrast=0.0,
        grain=0.18,
        vignette=0.22,
    ),
    "smoky_split_tone": LookAdjustments(
        intensity=1.0,
        warmth=0.0,
        contrast=0.0,
        grain=0.36,
        vignette=0.55,
    ),
}


def normalize_look_preset(value: object) -> LookPreset:
    """Return a known preset, defaulting only missing/blank legacy values."""
    if value is None or value == "":
        return DEFAULT_LOOK_PRESET
    if isinstance(value, str) and value in LOOK_PRESETS:
        return cast(LookPreset, value)
    raise ValueError(f"Unknown look preset: {value!r}")


def default_look_adjustments(value: object) -> LookAdjustments | None:
    """Return a fresh authored default for a customizable preset."""
    preset = normalize_look_preset(value)
    default = _LOOK_DEFAULTS.get(preset)
    return default.model_copy() if default is not None else None


def normalize_look_adjustments(
    preset_value: object,
    value: object,
) -> LookAdjustments | None:
    """Validate persisted controls, failing safe to the preset defaults.

    Route payloads are rejected by Pydantic before persistence. This tolerant
    boundary is for legacy or manually edited JSONB reaching a worker.
    """
    default = default_look_adjustments(preset_value)
    if default is None:
        return None
    if value is None:
        return default
    if isinstance(value, LookAdjustments):
        return value.model_copy()
    try:
        return LookAdjustments.model_validate(value)
    except (TypeError, ValidationError):
        return default


def _even(value: float, *, minimum: int = 2) -> int:
    rounded = max(minimum, int(round(value)))
    return rounded if rounded % 2 == 0 else rounded + 1


def _validate_filter_args(width: int, height: int, label_prefix: str) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("Look preset dimensions must be positive")
    if not label_prefix.replace("_", "").isalnum():
        raise ValueError("Look preset label prefix must be alphanumeric")


def _film_grade_filter(
    preset: Literal["olive_film", "smoky_split_tone"],
    *,
    width: int,
    height: int,
    adjustments: object = None,
    label_prefix: str = "film",
) -> str:
    """Build the reference-derived, footage-only cinematic grade.

    Olive Film mirrors the Instagram reference's yellow/olive highlights,
    green-cool shadows, restrained saturation, and soft highlight rolloff.
    Smoky Split-Tone mirrors the YouTube reference's stronger warm/teal split,
    deeper contrast, diffusion, grain, and vignette. It intentionally does not
    synthesize smoke: the heavy haze in that reference belongs to the footage.
    """
    _validate_filter_args(width, height, label_prefix)
    controls = normalize_look_adjustments(preset, adjustments)
    assert controls is not None

    strength = controls.intensity
    warmth = controls.warmth
    contrast_trim = controls.contrast

    if preset == "olive_film":
        black = 0.018 * strength
        shadow = 0.18 - 0.012 * strength
        mid = 0.50 - 0.006 * strength
        high = 0.82 + 0.018 * strength
        white = 1.0 - 0.018 * strength
        shadow_rgb = (-0.024 * strength, 0.034 * strength, 0.010 * strength)
        high_rgb = (0.055 * strength, 0.026 * strength, -0.038 * strength)
        base_contrast = 1.0 - 0.045 * strength
        saturation = 1.0 - 0.11 * strength
        gamma = 1.0 + 0.018 * strength
        softness = -0.18 * strength
        seed = 6413
    else:
        black = 0.010 * strength
        shadow = 0.18 - 0.020 * strength
        mid = 0.50 - 0.012 * strength
        high = 0.82 + 0.026 * strength
        white = 1.0 - 0.012 * strength
        shadow_rgb = (-0.038 * strength, 0.028 * strength, 0.052 * strength)
        high_rgb = (0.072 * strength, 0.020 * strength, -0.052 * strength)
        base_contrast = 1.0 + 0.055 * strength
        saturation = 1.0 - 0.08 * strength
        gamma = 1.0 - 0.012 * strength
        softness = -0.27 * strength
        seed = 8721

    # Warmth is a user trim around the authored split tone. Apply it across
    # shadows, midtones, and highlights so the slider remains perceptible on
    # both bright and dark footage without destroying the preset character.
    warm_shadow = 0.025 * warmth
    warm_mid = 0.030 * warmth
    warm_high = 0.035 * warmth
    contrast = max(0.72, min(1.28, base_contrast + contrast_trim * 0.20))

    filters = [
        "format=gbrp",
        (
            "curves=all='"
            f"0/{black:.4f} 0.18/{shadow:.4f} 0.50/{mid:.4f} "
            f"0.82/{high:.4f} 1/{white:.4f}'"
        ),
        (
            "colorbalance="
            f"rs={shadow_rgb[0] + warm_shadow:.4f}:"
            f"gs={shadow_rgb[1]:.4f}:"
            f"bs={shadow_rgb[2] - warm_shadow:.4f}:"
            f"rm={warm_mid:.4f}:gm=0.0000:bm={-warm_mid:.4f}:"
            f"rh={high_rgb[0] + warm_high:.4f}:"
            f"gh={high_rgb[1]:.4f}:"
            f"bh={high_rgb[2] - warm_high:.4f}"
        ),
        f"eq=contrast={contrast:.4f}:saturation={saturation:.4f}:gamma={gamma:.4f}",
        f"unsharp=5:5:{softness:.4f}:5:5:0.0000",
    ]
    if controls.vignette > 0.001:
        filters.append(f"vignette=angle={0.28 * controls.vignette:.4f}:eval=init")
    grain_strength = int(round(controls.grain * 12))
    if grain_strength > 0:
        filters.append(f"noise=alls={grain_strength}:allf=t+u:all_seed={seed}")
    return ",".join(filters)


def olive_film_filter(
    *,
    width: int,
    height: int,
    adjustments: object = None,
    label_prefix: str = "olive",
) -> str:
    return _film_grade_filter(
        "olive_film",
        width=width,
        height=height,
        adjustments=adjustments,
        label_prefix=label_prefix,
    )


def smoky_split_tone_filter(
    *,
    width: int,
    height: int,
    adjustments: object = None,
    label_prefix: str = "smoky",
) -> str:
    return _film_grade_filter(
        "smoky_split_tone",
        width=width,
        height=height,
        adjustments=adjustments,
        label_prefix=label_prefix,
    )


def golden_hour_filter(*, width: int, height: int, label_prefix: str = "golden") -> str:
    """Approved fixed warm grade for edit-wide application."""
    _validate_filter_args(width, height, label_prefix)
    return ",".join(
        [
            "eq=brightness=0.015:contrast=1.08:gamma=1.025",
            "colorcorrect=rl=0.005:bl=-0.015:rh=0.055:bh=-0.055:saturation=1.22",
            "convolution=0m='0 -0.05 0 -0.05 1.2 -0.05 0 -0.05 0'",
        ]
    )


def faded_analog_filter(*, width: int, height: int, label_prefix: str = "faded") -> str:
    """Approved fixed low-saturation film grade with deterministic grain."""
    _validate_filter_args(width, height, label_prefix)
    base_label = f"{label_prefix}_base"
    mask_label = f"{label_prefix}_mask"
    return ",".join(
        [
            "eq=brightness=0.045:contrast=0.93:saturation=0.76:gamma=1.025",
            "colorcorrect=rl=-0.010:bl=0.025:rh=0.040:bh=-0.035",
            "noise=alls=3:allf=u:all_seed=9321",
            (
                f"split=1[{base_label}];"
                f"movie=filename='{FADED_VIGNETTE_MASK_PATH}',"
                f"scale={width}:{height},format=yuv420p[{mask_label}];"
                f"[{base_label}][{mask_label}]"
                "blend=c0_mode=multiply:c1_mode=normal:c2_mode=normal"
            ),
        ]
    )


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
    _validate_filter_args(width, height, label_prefix)

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
    adjustments: object = None,
) -> str | None:
    """Resolve and build a preset filter; neutral is an exact bypass."""
    preset = normalize_look_preset(value)
    if preset == "none":
        return None
    if preset == "olive_film":
        return olive_film_filter(
            width=width,
            height=height,
            adjustments=adjustments,
            label_prefix=label_prefix,
        )
    if preset == "smoky_split_tone":
        return smoky_split_tone_filter(
            width=width,
            height=height,
            adjustments=adjustments,
            label_prefix=label_prefix,
        )
    if preset == "stadium_diffusion":
        return stadium_diffusion_filter(
            width=width,
            height=height,
            label_prefix=label_prefix,
        )
    if preset == "golden_hour":
        return golden_hour_filter(width=width, height=height, label_prefix=label_prefix)
    if preset == "faded_analog":
        return faded_analog_filter(width=width, height=height, label_prefix=label_prefix)
    assert_never(preset)
