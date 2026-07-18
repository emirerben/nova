"""Single-clip semantic boundary effects using target pixels only."""

from __future__ import annotations

import subprocess
from typing import Any

from app.pipeline.reframe import _encoding_args


class BoundaryEffectError(RuntimeError):
    pass


def build_boundary_effects_command(
    input_video: str,
    effects: list[dict[str, Any]],
    output_path: str,
) -> list[str]:
    """Build one FFmpeg pass for all horizontal motion-blur windows."""

    supported = [
        effect
        for effect in effects
        if effect.get("effect") == "horizontal_motion_blur"
        and float(effect.get("duration_s") or 0.0) > 0.0
    ]
    if not supported:
        raise BoundaryEffectError("no supported boundary effects")

    weights: list[str] = []
    blur_sigma = 1.0
    for effect in supported:
        start = max(0.0, float(effect.get("at_s") or 0.0))
        duration = max(0.12, min(1.2, float(effect.get("duration_s") or 0.42)))
        end = start + duration
        intensity = max(0.0, min(1.0, float(effect.get("intensity") or 1.0)))
        blur_sigma = max(
            blur_sigma,
            max(1.0, min(80.0, float(effect.get("blur_sigma") or 44.0))),
        )
        weights.append(
            "if("
            f"between(T\\,{start:.3f}\\,{end:.3f})\\,"
            f"sin(PI*(T-{start:.3f})/{duration:.3f})*{intensity:.3f}\\,"
            "0)"
        )
    weight = weights[0]
    for next_weight in weights[1:]:
        weight = f"max({weight}\\,{next_weight})"
    blend_expr = f"A*(1-({weight}))+B*({weight})"
    filter_complex = (
        "[0:v:0]split=2[boundary_clean][boundary_blur_src];"
        f"[boundary_blur_src]gblur=sigma={blur_sigma:.3f}:sigmaV=1:steps=6[boundary_blur];"
        f"[boundary_clean][boundary_blur]blend=all_expr='{blend_expr}':shortest=1[boundary_out]"
    )
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_video,
        "-filter_complex",
        filter_complex,
        "-map",
        "[boundary_out]",
        "-map",
        "0:a:0?",
        "-c:a",
        "copy",
        *_encoding_args(output_path, preset="fast", include_audio=False),
    ]


def apply_boundary_effects(
    input_video: str,
    effects: list[dict[str, Any]],
    output_path: str,
    *,
    timeout_s: int = 600,
) -> None:
    cmd = build_boundary_effects_command(input_video, effects, output_path)
    result = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise BoundaryEffectError(
            f"boundary effect render failed (rc={result.returncode}): {detail}"
        )
