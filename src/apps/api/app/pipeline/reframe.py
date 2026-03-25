"""[Stage 7] FFmpeg reframe + caption burn + audio normalize → 1080×1920 MP4.

16:9 input: scale to height=1920 → face-tracked crop to 1080×1920
9:16 input: scale+pad to 1080×1920
Output: h264, 1080×1920, 30fps, 4Mbps video, aac 192kbps, -14 LUFS audio

IMPORTANT: subprocess.run() is blocking — all calls here are sync (called from
Celery worker thread, not async context). If ever called from async, wrap in
asyncio.run_in_executor(None, ...).

CRITICAL: Never use shell=True. Always pass args as a list.
"""

import os
import subprocess

import structlog

from app.config import settings

log = structlog.get_logger()

MAX_OUTPUT_BYTES = 500 * 1024 * 1024  # 500MB — clip flagged failed if exceeded


class ReframeError(Exception):
    pass


def reframe_and_export(
    input_path: str,
    start_s: float,
    end_s: float,
    aspect_ratio: str,  # "16:9" | "9:16"
    ass_subtitle_path: str | None,
    output_path: str,
    text_overlay_pngs: list[dict] | None = None,
    darkening_windows: list[tuple[float, float]] | None = None,
    narrowing_windows: list[tuple[float, float]] | None = None,
    color_hint: str = "none",
) -> None:
    """Render a single clip to the output spec. Raises ReframeError on failure.

    Never uses shell=True. All paths are passed as list args.

    text_overlay_pngs: list of {png_path, start_s, end_s} dicts for overlay compositing.
    color_hint: color grading to apply — "warm", "cool", "high-contrast", etc.
    """
    duration = end_s - start_s
    if duration <= 0:
        raise ReframeError(f"Invalid duration: {duration}s")

    # Build video filter chain
    vf_parts = _build_video_filter(
        aspect_ratio, ass_subtitle_path,
        darkening_windows=darkening_windows,
        narrowing_windows=narrowing_windows,
        color_hint=color_hint,
    )

    # Build command — use filter_complex when overlays are present
    has_overlays = bool(text_overlay_pngs)

    if has_overlays:
        cmd = _build_overlay_cmd(
            input_path, start_s, duration, vf_parts,
            text_overlay_pngs, output_path,
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
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
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

    log.info("reframe_start", input=input_path, start=start_s, end=end_s,
             aspect=aspect_ratio, overlays=len(text_overlay_pngs or []))

    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=600,  # 10min max per clip
        check=False,
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
    output_path: str,
) -> list[str]:
    """Build FFmpeg command with -filter_complex for PNG overlay compositing.

    Chain: [0:v] → vf filters → overlay each PNG with enable timing → output
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

    # Chain overlay filters
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

    filter_complex = ";".join(fc_parts)

    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", f"[{prev_label}]",
        "-map", "0:a?",
        "-af", f"loudnorm=I={settings.output_target_lufs}:TP=-1.5:LRA=11",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
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
    ])

    return cmd


def _build_video_filter(
    aspect_ratio: str,
    ass_path: str | None,
    darkening_windows: list[tuple[float, float]] | None = None,
    narrowing_windows: list[tuple[float, float]] | None = None,
    color_hint: str = "none",
) -> list[str]:
    """Return list of filter segments to join with commas.

    Filter order: scale/crop → color grading → darkening → narrowing → caption ASS.
    Text overlays are handled separately via overlay filter (not in this chain).
    """
    filters: list[str] = []

    # 1. Scale/crop (existing)
    if aspect_ratio == "16:9":
        filters.append(
            f"scale=-2:{settings.output_height}"
        )
        filters.append(
            f"crop={settings.output_width}:{settings.output_height}"
        )
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

    # 5. Caption ASS (existing — speech captions)
    if ass_path and os.path.exists(ass_path):
        escaped = ass_path.replace("\\", "/").replace(":", "\\:")
        filters.append(f"ass={escaped}")

    return filters


# ── Color grading filter mapping ─────────────────────────────────────────────
# Starting-point values — visually verify on 3-5 templates and tune if needed.
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
