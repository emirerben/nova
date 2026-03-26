"""[Stage 7] FFmpeg reframe + caption burn + audio normalize → 1080×1920 MP4.

16:9 input: scale to height=1920 → face-tracked crop to 1080×1920
9:16 input: scale+pad to 1080×1920
Output: h264, 1080×1920, 30fps, 4Mbps video, aac 192kbps, -14 LUFS audio

Supports three overlay types in the same slot:
  1. PNG overlays (font-cycle, static) → FFmpeg overlay filter with enable timing
  2. ASS animated text (fade-in, typewriter, slide-up) → subtitles filter
  3. ASS captions (speech) → subtitles filter

Filter chain when overlays present:
  [0:v] → setpts (if speed≠1) → scale/crop → color_grade
    → [base] → overlay PNGs → subtitles ASS text → subtitles ASS captions → [out]

IMPORTANT: subprocess.run() is blocking — all calls here are sync.
CRITICAL: Never use shell=True. Always pass args as a list.
"""

import os
import subprocess

import structlog

from app.config import settings

log = structlog.get_logger()

MAX_OUTPUT_BYTES = 500 * 1024 * 1024  # 500MB


class ReframeError(Exception):
    pass


def _encoding_args(output_path: str) -> list[str]:
    """Shared FFmpeg output encoding arguments (DRY)."""
    return [
        "-c:v", "libx264",
        "-profile:v", "high",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",  # QuickTime/browser compatibility
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
) -> None:
    """Render a single clip to the output spec. Raises ReframeError on failure.

    text_overlay_pngs: list of {png_path, start_s, end_s} for PNG overlay compositing.
    ass_overlay_paths: list of ASS file paths for animated text overlays.
    speed_factor: playback speed multiplier (2.0 = 2x fast). Applied as setpts=PTS/speed_factor.
    color_hint: color grading preset — "warm", "cool", "high-contrast", etc.
    """
    duration = end_s - start_s
    if duration <= 0:
        raise ReframeError(f"Invalid duration: {duration}s")

    vf_parts = _build_video_filter(
        aspect_ratio, ass_subtitle_path,
        color_hint=color_hint,
        speed_factor=speed_factor,
        darkening_windows=darkening_windows,
        narrowing_windows=narrowing_windows,
    )

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
            "-af", f"loudnorm=I={settings.output_target_lufs}:TP=-1.5:LRA=11",
            "-vf", vf_string,
            *_encoding_args(output_path),
        ]

    log.info(
        "reframe_start", input=input_path, start=start_s, end=end_s,
        aspect=aspect_ratio, speed=speed_factor,
        overlays=len(text_overlay_pngs or []),
        ass_overlays=len(ass_overlay_paths or []),
    )

    result = subprocess.run(cmd, capture_output=True, timeout=600, check=False)

    if result.returncode != 0 and ass_overlay_paths:
        # ASS overlays may fail if FFmpeg lacks libass — retry without them
        log.warning(
            "reframe_ass_fallback",
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

    Chain: [0:v] → vf filters → overlay each PNG → subtitles each ASS → output
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

    # Build filter_complex
    vf_chain = ",".join(vf_parts) if vf_parts else "null"
    fc_parts = [f"[0:v]{vf_chain}[base]"]

    # Chain PNG overlay filters
    prev_label = "base"
    for i, ov in enumerate(overlay_pngs):
        input_idx = i + 1
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
    for j, ass_path in enumerate(ass_overlay_paths):
        escaped = ass_path.replace("\\", "/").replace(":", "\\:")
        out_label = f"sub{j}"
        fc_parts.append(f"[{prev_label}]subtitles={escaped}[{out_label}]")
        prev_label = out_label

    filter_complex = ";".join(fc_parts)

    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", f"[{prev_label}]",
        "-map", "0:a?",
        "-af", f"loudnorm=I={settings.output_target_lufs}:TP=-1.5:LRA=11",
        *_encoding_args(output_path),
    ])

    return cmd


def _build_video_filter(
    aspect_ratio: str,
    ass_path: str | None,
    color_hint: str = "none",
    speed_factor: float = 1.0,
    darkening_windows: list[tuple[float, float]] | None = None,
    narrowing_windows: list[tuple[float, float]] | None = None,
) -> list[str]:
    """Return list of filter segments to join with commas.

    Filter order: setpts (speed) → scale/crop → color grading
                  → darkening → narrowing → caption ASS.
    setpts MUST be first to normalize PTS before timed filters (darkening, narrowing).
    """
    filters: list[str] = []

    # 0. Speed ramp — FIRST filter to normalize PTS for all subsequent timed filters
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

    # 2. Color grading
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

    # 5. Caption ASS (speech captions — distinct from text overlay ASS)
    if ass_path and os.path.exists(ass_path):
        escaped = ass_path.replace("\\", "/").replace(":", "\\:")
        filters.append(f"ass={escaped}")

    return filters


# ── Color grading filter mapping ─────────────────────────────────────────────

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
    """Return FFmpeg filter strings for the given color hint."""
    return list(_COLOR_HINT_MAP.get(color_hint, []))
