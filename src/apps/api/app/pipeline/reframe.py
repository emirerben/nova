"""[Stage 7] FFmpeg reframe + caption burn + audio normalize -> 1080x1920 MP4.

16:9 input: scale to height=1920 -> face-tracked crop to 1080x1920
9:16 input: scale+pad to 1080x1920
Output: h264, 1080x1920, 30fps, 4Mbps video, aac 192kbps, -14 LUFS audio

Supports three overlay types in the same slot:
  1. PNG overlays (font-cycle, static) -> FFmpeg overlay filter with enable timing
  2. ASS animated text (fade-in, typewriter, slide-up) -> subtitles filter
  3. ASS captions (speech) -> subtitles filter

Filter chain when overlays present:
  [0:v] -> setpts (if speed!=1) -> scale/crop -> color_grade
    -> [base] -> overlay PNGs -> subtitles ASS text -> subtitles ASS captions -> [out]

IMPORTANT: subprocess.run() is blocking -- all calls here are sync.
CRITICAL: Never use shell=True. Always pass args as a list.
"""

import os
import subprocess

import structlog

from app.config import settings
from app.pipeline.text_overlay import FONTS_DIR

log = structlog.get_logger()

MAX_OUTPUT_BYTES = 500 * 1024 * 1024  # 500MB


class ReframeError(Exception):
    pass


def _encoding_args(
    output_path: str,
    preset: str = "fast",
    crf: str = "18",
) -> list[str]:
    """Shared FFmpeg output encoding arguments (DRY).

    Args:
        preset: libx264 preset. Use "ultrafast" for intermediate files
                that will be re-encoded later, "fast" for final output.
        crf: Constant Rate Factor. Lower = better quality.
             18 is visually lossless, 23 is default (too lossy for
             multi-pass pipelines with 3+ re-encodes).

    Color handling: HDR/HLG sources (bt2020, arib-std-b67) are tone-mapped
    to SDR (bt709) via zscale to prevent washed-out colors when forcing
    yuv420p output.
    """
    return [
        "-c:v", "libx264",
        "-profile:v", "high",
        "-preset", preset,
        "-crf", crf,
        "-pix_fmt", "yuv420p",
        # Re-enable scene-change keyframe insertion (ultrafast preset sets
        # scenecut=0, disabling it). Without keyframes at scene transitions,
        # players show horizontal tearing when decoding across slot
        # boundaries — the top half shows the old scene, bottom the new.
        # keyint=90 = max 3s between keyframes at 30fps (safety net).
        "-x264-params", "scenecut=40:keyint=90",
        # Tag output as bt709 (SDR) so players render colors correctly.
        # iPhone clips often come as bt2020/HLG; without explicit tagging
        # the encoder copies source tags → player misinterprets SDR data
        # as HDR → washed-out look.
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-b:v", settings.output_video_bitrate,
        "-maxrate", settings.output_video_bitrate,
        "-bufsize", "8M",
        "-r", str(settings.output_fps),
        "-c:a", "aac",
        "-b:a", settings.output_audio_bitrate,
        "-s", f"{settings.output_width}x{settings.output_height}",
        "-movflags", "+faststart",
        "-y",
        output_path,
    ]


def reframe_and_export(
    input_path: str,
    start_s: float,
    end_s: float,
    aspect_ratio: str,  # "16:9" | "9:16"
    ass_subtitle_path: str | None,
    output_path: str,
    text_overlay_pngs: list[dict] | None = None,
    ass_overlay_paths: list[str] | None = None,
    color_hint: str = "none",
    speed_factor: float = 1.0,
    darkening_windows: list[tuple[float, float]] | None = None,
    narrowing_windows: list[tuple[float, float]] | None = None,
    has_grid: bool = False,
    grid_color: str = "#FFFFFF",
    grid_opacity: float = 0.6,
    grid_thickness: int = 3,
) -> None:
    """Render a single clip to the output spec. Raises ReframeError on failure.

    Never uses shell=True. All paths are passed as list args.

    text_overlay_pngs: list of {png_path, start_s, end_s} for PNG overlay compositing.
    ass_overlay_paths: list of ASS file paths for animated text overlays.
    speed_factor: playback speed multiplier (2.0 = 2x fast). Applied as setpts=PTS/speed_factor.
    color_hint: color grading preset -- "warm", "cool", "high-contrast", etc.
    """
    duration = end_s - start_s
    if duration <= 0:
        raise ReframeError(f"Invalid duration: {duration}s")

    # Build video filter chain
    vf_parts = _build_video_filter(
        aspect_ratio, ass_subtitle_path,
        color_hint=color_hint,
        speed_factor=speed_factor,
        darkening_windows=darkening_windows,
        narrowing_windows=narrowing_windows,
        has_grid=has_grid,
        grid_color=grid_color,
        grid_opacity=grid_opacity,
        grid_thickness=grid_thickness,
    )

    # Build command -- use filter_complex when overlays are present
    has_overlays = bool(text_overlay_pngs) or bool(ass_overlay_paths)

    if has_overlays:
        cmd = _build_overlay_cmd(
            input_path, start_s, duration, vf_parts,
            text_overlay_pngs or [],
            ass_overlay_paths or [],
            output_path,
        )
    else:
        vf_string = ",".join(vf_parts)
        cmd = [
            "ffmpeg",
            "-ss", str(start_s),
            "-t", str(duration),
            "-i", input_path,
            "-vf", vf_string,
            *_encoding_args(output_path, preset="ultrafast"),
        ]

    log.info(
        "reframe_start", input=input_path, start=start_s, end=end_s,
        aspect=aspect_ratio, speed=speed_factor,
        overlays=len(text_overlay_pngs or []),
        ass_overlays=len(ass_overlay_paths or []),
    )

    result = subprocess.run(cmd, capture_output=True, timeout=600, check=False)

    if result.returncode != 0 and ass_overlay_paths:
        # ASS overlays may fail if FFmpeg lacks libass -- retry with PNGs only
        log.warning(
            "reframe_ass_fallback_to_png",
            rc=result.returncode,
            ass_count=len(ass_overlay_paths),
        )
        return reframe_and_export(
            input_path=input_path,
            start_s=start_s,
            end_s=end_s,
            aspect_ratio=aspect_ratio,
            ass_subtitle_path=ass_subtitle_path,
            output_path=output_path,
            text_overlay_pngs=text_overlay_pngs,
            ass_overlay_paths=None,  # retry without ASS
            color_hint=color_hint,
            speed_factor=speed_factor,
            darkening_windows=darkening_windows,
            narrowing_windows=narrowing_windows,
            has_grid=has_grid,
            grid_color=grid_color,
            grid_opacity=grid_opacity,
            grid_thickness=grid_thickness,
        )

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[:500]
        raise ReframeError(f"FFmpeg failed (rc={result.returncode}): {stderr}")

    if not os.path.exists(output_path):
        raise ReframeError("FFmpeg exited 0 but output file not found")

    file_size = os.path.getsize(output_path)
    if file_size > MAX_OUTPUT_BYTES:
        os.unlink(output_path)
        raise ReframeError(f"Output exceeds 500MB ({file_size} bytes)")

    log.info("reframe_done", output=output_path, size_mb=file_size // (1024 * 1024))


def _build_overlay_cmd(
    input_path: str,
    start_s: float,
    duration: float,
    vf_parts: list[str],
    overlay_pngs: list[dict],
    ass_overlay_paths: list[str],
    output_path: str,
) -> list[str]:
    """Build FFmpeg command with -filter_complex for PNG + ASS overlay compositing.

    Chain: [0:v] -> vf filters -> overlay each PNG -> subtitles each ASS -> output
    """
    cmd = [
        "ffmpeg",
        "-ss", str(start_s),
        "-t", str(duration),
        "-i", input_path,
    ]

    # Add each PNG as an input
    for ov in overlay_pngs:
        cmd.extend(["-i", ov["png_path"]])

    # Build filter_complex string
    # First apply the vf chain to the video input
    vf_chain = ",".join(vf_parts) if vf_parts else "null"
    fc_parts = [f"[0:v]{vf_chain}[base]"]

    # Chain PNG overlay filters
    prev_label = "base"
    for i, ov in enumerate(overlay_pngs):
        input_idx = i + 1  # PNG inputs start at index 1
        start = ov["start_s"]
        end = ov["end_s"]
        out_label = f"ov{i}"
        fc_parts.append(
            f"[{prev_label}][{input_idx}:v]overlay=0:0"
            f":enable='between(t,{start:.3f},{end:.3f})'"
            f"[{out_label}]"
        )
        prev_label = out_label

    # Chain ASS subtitle filters for animated text overlays
    escaped_fonts = FONTS_DIR.replace("\\", "/").replace(":", "\\:")
    for j, ass_path in enumerate(ass_overlay_paths):
        escaped = ass_path.replace("\\", "/").replace(":", "\\:")
        out_label = f"sub{j}"
        fc_parts.append(
            f"[{prev_label}]subtitles={escaped}:fontsdir={escaped_fonts}[{out_label}]"
        )
        prev_label = out_label

    filter_complex = ";".join(fc_parts)

    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", f"[{prev_label}]",
        "-map", "0:a?",
        *_encoding_args(output_path, preset="ultrafast"),
    ])

    return cmd


def _build_video_filter(
    aspect_ratio: str,
    ass_path: str | None,
    color_hint: str = "none",
    speed_factor: float = 1.0,
    darkening_windows: list[tuple[float, float]] | None = None,
    narrowing_windows: list[tuple[float, float]] | None = None,
    has_grid: bool = False,
    grid_color: str = "#FFFFFF",
    grid_opacity: float = 0.6,
    grid_thickness: int = 3,
) -> list[str]:
    """Return list of filter segments to join with commas.

    Filter order: setpts (speed) -> scale/crop -> color grading
                  -> darkening -> narrowing -> grid -> caption ASS.
    setpts MUST be first to normalize PTS before timed filters (darkening, narrowing).
    Text overlays are handled separately via overlay filter (not in this chain).
    Grid (rule-of-thirds) is drawn after visual adjustments but before captions so it
    sits above the footage and behind any text overlays burned in post-assembly.
    """
    filters: list[str] = []

    # 0a. HDR/HLG → SDR color conversion.
    # iPhone clips are bt2020/HLG; without conversion, colors look washed out
    # after encoding to bt709 yuv420p. This is safe on bt709 sources (no-op).
    filters.append("colorspace=all=bt709:iall=bt2020")

    # 0. Speed ramp -- FIRST filter to normalize PTS for all subsequent timed filters
    if speed_factor != 1.0 and speed_factor > 0:
        filters.append(f"setpts=PTS/{speed_factor}")

    # 1. Scale/crop
    if aspect_ratio == "16:9":
        filters.append(f"scale=-2:{settings.output_height}")
        filters.append(f"crop={settings.output_width}:{settings.output_height}")
    else:
        filters.append(
            f"scale={settings.output_width}:{settings.output_height}"
            f":force_original_aspect_ratio=decrease"
        )
        filters.append(
            f"pad={settings.output_width}:{settings.output_height}"
            f":(ow-iw)/2:(oh-ih)/2"
        )

    # 2. Color grading (applied before darkening so darkened areas have the grade)
    for f in _color_hint_filters(color_hint):
        filters.append(f)

    # 3. Darkening (colorlevels with enable timing)
    for start, end in (darkening_windows or []):
        filters.append(
            f"colorlevels=rimax=0.45:gimax=0.45:bimax=0.45"
            f":enable='between(t,{start:.3f},{end:.3f})'"
        )

    # 4. Narrowing (letterbox drawbox bars, top + bottom)
    for start, end in (narrowing_windows or []):
        enable = f"enable='between(t,{start:.3f},{end:.3f})'"
        filters.append(
            f"drawbox=x=0:y=0:w=iw:h=120:color=black@0.85:t=fill:{enable}"
        )
        filters.append(
            f"drawbox=x=0:y=ih-120:w=iw:h=120:color=black@0.85:t=fill:{enable}"
        )

    # 5. Rule-of-thirds grid overlay (per-slot)
    if has_grid:
        # FFmpeg drawgrid: one cell = iw/3 x ih/3, drawn at every 1/3 boundary.
        # Hex → FFmpeg color spec: strip "#" and append @opacity.
        color_hex = grid_color.lstrip("#")
        filters.append(
            f"drawgrid=w=iw/3:h=ih/3:t={grid_thickness}"
            f":c=0x{color_hex}@{grid_opacity:.2f}"
        )

    # 6. Caption ASS (speech captions -- distinct from text overlay ASS)
    if ass_path and os.path.exists(ass_path):
        escaped = ass_path.replace("\\", "/").replace(":", "\\:")
        escaped_fonts = FONTS_DIR.replace("\\", "/").replace(":", "\\:")
        filters.append(f"ass={escaped}:fontsdir={escaped_fonts}")

    return filters


# -- Color grading filter mapping ---------------------------------------------
# Starting-point values -- visually verify on 3-5 templates and tune if needed.
# These are intentionally subtle to avoid over-processing.

_COLOR_HINT_MAP: dict[str, list[str]] = {
    "warm": ["colorbalance=rs=0.15:gs=0.05:bs=-0.1"],
    "cool": ["colorbalance=rs=-0.1:gs=0.0:bs=0.15"],
    "high-contrast": ["eq=contrast=1.3:saturation=1.2"],
    "desaturated": ["eq=saturation=0.6"],
    "vintage": [
        "colorbalance=rs=0.1:gs=0.05:bs=-0.15",
        "eq=saturation=0.8",
    ],
}


def _color_hint_filters(color_hint: str) -> list[str]:
    """Return FFmpeg filter strings for the given color hint.

    Returns empty list for "none" or unrecognized values.
    """
    return list(_COLOR_HINT_MAP.get(color_hint, []))
