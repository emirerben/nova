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
    color_trc: str = "bt709"  # e.g. "arib-std-b67" (HLG), "smpte2084" (PQ/HDR10)
    color_transfer: str = ""  # raw container value; empty = unknown/SDR
    # Pixel format from ffprobe (e.g. "yuv420p", "yuv420p10le"). Empty = unknown.
    # Used by the upload-time pre-flight in routes/template_jobs.py to reject
    # 10-bit HEVC clips longer than a hardware-cost ceiling — see the
    # MAX_HDR_DURATION_S constant there for the empirical budget.
    pix_fmt: str = ""


class ProbeError(Exception):
    pass


class ProbeTimeout(ProbeError):
    pass


def probe_video(file_path: str) -> VideoProbe:
    """Run ffprobe on file_path and return VideoProbe.

    Accepts a local filesystem path OR an https:// URL (e.g. a signed GCS
    URL). For URLs, ffprobe range-reads the moov atom (~1-2s for a 400 MB
    clip) instead of streaming the whole file.

    Raises ProbeError on corrupt/unreadable file.
    Raises ProbeTimeout if ffprobe hangs beyond PROBE_TIMEOUT_S.
    Never uses shell=True.
    """
    # -rw_timeout caps how long ffmpeg's HTTP demuxer waits on a single
    # read/write before failing. Without it, a slow-stall TCP read can occupy
    # this thread for the full PROBE_TIMEOUT_S (60s). 5s is enough headroom
    # for typical GCS range requests (~200-500ms) while preventing a
    # malicious or unhealthy URL from holding a worker indefinitely. No
    # effect on local-file probes — they don't use the network demuxer.
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-rw_timeout",
        "5000000",  # microseconds — 5 seconds
        "-print_format",
        "json",
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
        color_trc_raw = video_stream.get("color_trc", "") or ""
        color_primaries = video_stream.get("color_primaries", "") or ""
        # HEVC/Dolby Vision mis-tag detection: iPhone HLG clips sometimes surface
        # with color_primaries=bt2020 but color_trc=bt709 (or empty) in container
        # metadata, depending on the ffprobe build. bt2020 primaries with non-HDR
        # trc is physically impossible for valid SDR — treat as HLG.
        if color_primaries == "bt2020" and color_trc_raw in ("", "bt709", "unknown", "reserved"):
            color_trc = "arib-std-b67"
        else:
            color_trc = color_trc_raw or "bt709"

        # Parse fps from "num/den" rational string
        fps_str = video_stream.get("r_frame_rate", "30/1")
        num, den = fps_str.split("/")
        fps = float(num) / float(den) if float(den) > 0 else 30.0

        # Color transfer for HDR detection downstream.
        # iPhone HDR clips report "arib-std-b67" (HLG); Apple/Sony PQ clips
        # report "smpte2084". SDR clips report "bt709", "bt470bg", or are
        # unset entirely. Empty string here means "unknown — treat as SDR".
        color_transfer = str(video_stream.get("color_transfer") or "").lower()
        pix_fmt = str(video_stream.get("pix_fmt") or "").lower()
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise ProbeError(f"Failed to parse probe metadata: {exc}") from exc

    aspect_ratio = _classify_aspect(width, height)

    # Never log the full URL when probing a signed GCS URL — the v4 query
    # string contains the time-limited signature, and even a 5-min TTL is
    # long enough to be replayed after the URL hits Fly logs / shippers /
    # error trackers. Strip the query for logging only; ffprobe already
    # consumed the signed URL upstream.
    log_path = file_path.split("?", 1)[0] if file_path.startswith("http") else file_path

    log.info(
        "probe_complete",
        path=log_path,
        duration_s=duration_s,
        resolution=f"{width}x{height}",
        aspect_ratio=aspect_ratio,
        fps=fps,
        # Raw rate strings catch inputs where `fps` was defaulted to 30 because
        # the source reports unparseable rates ("1/0", "0/0"). Without this,
        # downstream xfade-rejects-1/0 failures look like normal 30fps clips
        # in the logs. Added 2026-05-18 after prod job 856daa32-… (BAD BUNNY
        # music template) crashed on `xfade rate 1/0`.
        r_frame_rate=video_stream.get("r_frame_rate"),
        avg_frame_rate=video_stream.get("avg_frame_rate"),
        codec=codec,
        color_trc=color_trc,
        color_transfer=color_transfer or "unknown",
        pix_fmt=pix_fmt or "unknown",
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
        color_trc=color_trc,
        color_transfer=color_transfer,
        pix_fmt=pix_fmt,
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
