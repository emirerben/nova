"""Audio preprocessing helpers shared by the admin upload route and the
Whisper lyrics service.

Two concerns:
  - Detect whether a media file contains a video stream (the admin upload
    accepts ``video/mp4`` so a stray YouTube .mp4 lands here intact).
  - Re-mux or re-encode to a Whisper-friendly, storage-friendly audio-only
    container. Strip-video (``-vn -c:a copy``) is lossless and sub-second.
    Mono 64 kbps AAC is the fallback for genuinely-large pure-audio files.

Distinct from ``audio_download.py`` (yt-dlp-shaped, network-bound) — these
helpers operate on files already on local disk.
"""

from __future__ import annotations

import json
import os
import subprocess

import structlog

log = structlog.get_logger()


class AudioPreprocessError(Exception):
    """Raised when ffprobe/ffmpeg fails. Message is suitable for surfacing
    to an admin (includes ffmpeg stderr tail).
    """


def _safe_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def has_video_stream(path: str) -> bool:
    """Return True iff ffprobe finds a real video stream in *path*.

    Attached-picture streams (m4a/mp4 cover art encoded as MJPEG/PNG with
    ``disposition.attached_pic=1``) are NOT considered video — stripping
    them would silently destroy cover art on legitimate audio uploads, and
    they don't bloat file size enough to be worth re-muxing.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout or "{}")
        return any(
            s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic") != 1
            for s in data.get("streams", [])
        )
    except Exception as exc:
        # ffprobe missing or returning garbage. Returning False is safer
        # (avoids crashing the request) but log loudly: a silent False here
        # turns the upload-strip path into a no-op and lets video-bearing
        # bytes land in GCS unchanged.
        log.warning("audio_preprocess_ffprobe_failed", path=path, error=str(exc))
        return False


def strip_video(src: str, dest: str) -> None:
    """Re-mux *src* to *dest* keeping only the first audio stream. Lossless.

    Uses ``ffmpeg -vn -c:a copy -map 0:a:0 -f mp4`` so the audio bytes are
    untouched. ``0:a:0`` picks a single audio stream deterministically — a
    multi-track YouTube container (dubbed languages) would otherwise hand
    Whisper a stream chosen by SDK/server tie-break, which can swap between
    runs. Output container is mp4/m4a regardless of input.

    Raises AudioPreprocessError on ffmpeg failure (includes stderr tail).
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            src,
            "-vn",
            "-map",
            "0:a:0",
            "-c:a",
            "copy",
            "-f",
            "mp4",
            dest,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise AudioPreprocessError(
            f"ffmpeg strip-video failed (rc={result.returncode}): {(result.stderr or '')[-500:]}"
        )
    log.info(
        "audio_preprocess_strip_video",
        src_bytes=_safe_size(src),
        dest_bytes=_safe_size(dest),
    )


def compress_to_mono_64k(src: str, dest: str) -> None:
    """Re-encode *src* to mono 64 kbps AAC in an mp4 container.

    Whisper transcribes mono 64k vocals reliably. This is the fallback for
    pure-audio inputs that are still over the API ceiling even after
    strip-video (e.g., long uncompressed wav uploads).

    Raises AudioPreprocessError on ffmpeg failure.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            src,
            "-vn",
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-b:a",
            "64k",
            "-c:a",
            "aac",
            "-f",
            "mp4",
            dest,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise AudioPreprocessError(
            f"ffmpeg mono-64k compress failed (rc={result.returncode}): "
            f"{(result.stderr or '')[-500:]}"
        )
    log.info(
        "audio_preprocess_compress_mono_64k",
        src_bytes=_safe_size(src),
        dest_bytes=_safe_size(dest),
    )
