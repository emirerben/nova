"""[Stage 1] FFmpeg probe — extract video metadata without buffering any frames."""

import json
import subprocess
from dataclasses import dataclass

import structlog

log = structlog.get_logger()

PROBE_TIMEOUT_S = 60


@dataclass
class VideoProbe:
    duration_s: float
    fps: float
    width: int
    height: int
    has_audio: bool
    codec: str
    aspect_ratio: str  # "16:9" | "9:16" | "other"
    file_size_bytes: int


class ProbeError(Exception):
    pass


class ProbeTimeout(ProbeError):
    pass


def probe_video(file_path: str) -> VideoProbe:
    """Run ffprobe on file_path and return VideoProbe.

    Raises ProbeError on corrupt/unreadable file.
    Raises ProbeTimeout if ffprobe hangs beyond PROBE_TIMEOUT_S.
    Never uses shell=True.
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        file_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,  # we check manually below
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeTimeout(f"ffprobe timed out after {PROBE_TIMEOUT_S}s") from exc
    except FileNotFoundError as exc:
        raise ProbeError("ffprobe not found — is FFmpeg installed?") from exc

    if result.returncode != 0:
        raise ProbeError(f"ffprobe failed (rc={result.returncode}): {result.stderr[:300]}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe output is not valid JSON: {result.stdout[:200]}") from exc

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video_stream is None:
        raise ProbeError("No video stream found — file may be corrupt or not a video")

    try:
        duration_s = float(fmt.get("duration") or video_stream.get("duration", 0))
        width = int(video_stream["width"])
        height = int(video_stream["height"])
        codec = video_stream.get("codec_name", "unknown")
        file_size_bytes = int(fmt.get("size", 0))

        # Parse fps from "num/den" rational string
        fps_str = video_stream.get("r_frame_rate", "30/1")
        num, den = fps_str.split("/")
        fps = float(num) / float(den) if float(den) > 0 else 30.0
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise ProbeError(f"Failed to parse probe metadata: {exc}") from exc

    aspect_ratio = _classify_aspect(width, height)

    log.info(
        "probe_complete",
        path=file_path,
        duration_s=duration_s,
        resolution=f"{width}x{height}",
        aspect_ratio=aspect_ratio,
        fps=fps,
        codec=codec,
    )

    return VideoProbe(
        duration_s=duration_s,
        fps=fps,
        width=width,
        height=height,
        has_audio=audio_stream is not None,
        codec=codec,
        aspect_ratio=aspect_ratio,
        file_size_bytes=file_size_bytes,
    )


def _classify_aspect(width: int, height: int) -> str:
    if width == 0 or height == 0:
        return "other"
    ratio = width / height
    if 1.7 <= ratio <= 1.8:  # ~16:9
        return "16:9"
    if 0.55 <= ratio <= 0.57:  # ~9:16
        return "9:16"
    return "other"
