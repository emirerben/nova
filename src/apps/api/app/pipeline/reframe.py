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
import tempfile

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
) -> None:
    """Render a single clip to the output spec. Raises ReframeError on failure.

    Never uses shell=True. All paths are passed as list args.
    """
    duration = end_s - start_s
    if duration <= 0:
        raise ReframeError(f"Invalid duration: {duration}s")

    # Build video filter chain
    vf_parts = _build_video_filter(aspect_ratio, ass_subtitle_path)
    vf_string = ",".join(vf_parts)

    cmd = [
        "ffmpeg",
        "-ss", str(start_s),
        "-t", str(duration),
        "-i", input_path,
        # Audio normalization to -14 LUFS (EBU R128)
        "-af", f"loudnorm=I={settings.output_target_lufs}:TP=-1.5:LRA=11",
        # Video filter (reframe + optional captions)
        "-vf", vf_string,
        # Output codec settings
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-b:v", settings.output_video_bitrate,
        "-maxrate", settings.output_video_bitrate,
        "-bufsize", "8M",
        "-r", str(settings.output_fps),
        "-c:a", "aac",
        "-b:a", settings.output_audio_bitrate,
        # Force exact output dimensions
        "-s", f"{settings.output_width}x{settings.output_height}",
        "-movflags", "+faststart",  # web-optimized: moov atom at start
        "-y",
        output_path,
    ]

    log.info("reframe_start", input=input_path, start=start_s, end=end_s, aspect=aspect_ratio)

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


def _build_video_filter(aspect_ratio: str, ass_path: str | None) -> list[str]:
    """Return list of filter segments to join with commas."""
    filters: list[str] = []

    if aspect_ratio == "16:9":
        # Scale landscape to height=1920, then crop width to 1080 (center crop)
        # For face tracking we'd use a more complex approach; center crop is v1
        filters.append(
            f"scale=-2:{settings.output_height}"  # maintain aspect, fit height
        )
        filters.append(
            f"crop={settings.output_width}:{settings.output_height}"  # center crop to 1080
        )
    else:
        # 9:16 input: scale+pad to exact output dimensions
        filters.append(
            f"scale={settings.output_width}:{settings.output_height}"
            f":force_original_aspect_ratio=decrease"
        )
        filters.append(
            f"pad={settings.output_width}:{settings.output_height}"
            f":(ow-iw)/2:(oh-ih)/2"
        )

    if ass_path and os.path.exists(ass_path):
        # Escape path for ASS filter (backslashes and colons need escaping on Windows,
        # but on Linux/Mac just escape colons in paths)
        escaped = ass_path.replace("\\", "/").replace(":", "\\:")
        filters.append(f"ass={escaped}")

    return filters
