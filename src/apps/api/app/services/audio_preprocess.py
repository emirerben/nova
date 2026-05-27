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
import subprocess

import structlog

log = structlog.get_logger()


class AudioPreprocessError(Exception):
    """Raised when ffprobe/ffmpeg fails. Message is suitable for surfacing
    to an admin (includes ffmpeg stderr tail).
    """


def has_video_stream(path: str) -> bool:
    """Return True iff ffprobe finds at least one video stream in *path*."""
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
        return any(s.get("codec_type") == "video" for s in data.get("streams", []))
    except Exception:
        return False


def strip_video(src: str, dest: str) -> None:
    """Re-mux *src* to *dest* keeping only the audio stream. Lossless.

    Uses ``ffmpeg -vn -c:a copy -map 0:a -f mp4`` so the audio bytes are
    untouched. Output container is mp4/m4a regardless of input.

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
            "0:a",
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
            "0:a",
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
