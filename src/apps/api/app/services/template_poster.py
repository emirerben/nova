"""Extract a poster JPEG from a template video and upload it to GCS.

Single FFmpeg subprocess call (NEVER MoviePy / VideoFileClip — see CLAUDE.md).
Writes to GCS via upload_bytes_public_read; returns the GCS object path so the
caller can persist it on VideoTemplate.thumbnail_gcs_path.
"""

from __future__ import annotations

import subprocess

import structlog

from app.storage import upload_bytes_public_read

log = structlog.get_logger()

POSTER_SEEK_S = 1.5
POSTER_WIDTH = 540
POSTER_QUALITY = 4  # ffmpeg -q:v scale: 1=best, 31=worst. 4 ≈ 30-60KB at 540px.
FFMPEG_TIMEOUT_S = 30


class PosterExtractionError(RuntimeError):
    """FFmpeg returned a non-zero exit code or produced no output."""


def extract_poster_bytes(local_video_path: str) -> bytes:
    """Run FFmpeg to grab a single JPEG frame from the video.

    Pipes output to stdout so we never write to disk. Raises
    PosterExtractionError on FFmpeg failure, timeout, empty output, or output
    that doesn't begin with the JPEG magic bytes (corrupt encode).
    """
    # Defense in depth: even though local_video_path is always a tempfile path
    # under our control, prepend "--" so FFmpeg can't parse a leading "-" in
    # the filename as a flag.
    cmd = [
        "ffmpeg",
        "-ss", str(POSTER_SEEK_S),
        "-i", local_video_path,
        "-frames:v", "1",
        "-vf", f"scale={POSTER_WIDTH}:-1",
        "-q:v", str(POSTER_QUALITY),
        "-f", "mjpeg",
        "--",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise PosterExtractionError(
            f"ffmpeg timed out after {FFMPEG_TIMEOUT_S}s"
        ) from exc
    if result.returncode != 0 or not result.stdout:
        stderr = result.stderr.decode("utf-8", errors="replace")[:500]
        raise PosterExtractionError(
            f"ffmpeg returned {result.returncode}, stderr={stderr!r}"
        )
    # Guard against a successful exit that produced corrupt output (e.g. one
    # byte). JPEG always starts with FF D8 FF.
    if not result.stdout.startswith(b"\xff\xd8\xff"):
        raise PosterExtractionError(
            f"ffmpeg output is not a valid JPEG (got {len(result.stdout)} bytes)"
        )
    return result.stdout


def generate_and_upload(template_id: str, local_video_path: str) -> str:
    """Extract a poster from the local video file and upload it to GCS.

    Returns the GCS object path (suitable for VideoTemplate.thumbnail_gcs_path).
    Raises PosterExtractionError on FFmpeg failure; propagates GCS exceptions.
    """
    poster_bytes = extract_poster_bytes(local_video_path)
    gcs_path = f"templates/{template_id}/poster.jpg"
    upload_bytes_public_read(poster_bytes, gcs_path, content_type="image/jpeg")
    log.info(
        "template_poster_uploaded",
        template_id=template_id,
        gcs_path=gcs_path,
        bytes=len(poster_bytes),
    )
    return gcs_path
