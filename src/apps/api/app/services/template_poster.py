"""Extract a poster JPEG from a template video and upload it to GCS.

Single FFmpeg subprocess call (NEVER MoviePy / VideoFileClip — see CLAUDE.md).
Writes to GCS via upload_bytes_public_read; returns the GCS object path so the
caller can persist it on VideoTemplate.thumbnail_gcs_path.

The first 1.5s of some templates is a fade-in, so the historical fixed-seek
strategy produced near-black thumbnails. We now try a small sequence of seek
offsets and pick the first frame whose mean luma and luma stddev clear minimum
thresholds — if all fall short, we return the brightest attempt so we never
silently emit a black frame.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

import structlog

from app.storage import upload_bytes_public_read

log = structlog.get_logger()

# Ordered list of seek offsets the extractor will try until one clears the
# brightness/variance thresholds. The first entry is the historical default
# (1.5s); the rest are escape hatches for fade-in intros.
POSTER_SEEK_ATTEMPTS_S: tuple[float, ...] = (1.5, 3.0, 5.0, 10.0)
# Mean luma (Y channel, 0-255). YAVG below this looks visually black/very dark.
MIN_POSTER_LUMA = 35.0
# Stddev of luma — low values indicate a uniform-color frame (likely a fade
# midpoint or a solid-color hold) even when the mean isn't pitch-black.
MIN_POSTER_VARIANCE = 8.0
POSTER_WIDTH = 540
POSTER_QUALITY = 4  # ffmpeg -q:v scale: 1=best, 31=worst. 4 ≈ 30-60KB at 540px.
FFMPEG_TIMEOUT_S = 30

_YAVG_RE = re.compile(r"YAVG:\s*([\d.]+)")
_YDEV_RE = re.compile(r"YDEV:\s*([\d.]+)")


class PosterExtractionError(RuntimeError):
    """FFmpeg returned a non-zero exit code or produced no output."""


@dataclass(frozen=True)
class _PosterAttempt:
    seek_s: float
    jpeg: bytes
    luma_mean: float
    luma_stddev: float

    def passes_threshold(self) -> bool:
        return self.luma_mean >= MIN_POSTER_LUMA and self.luma_stddev >= MIN_POSTER_VARIANCE


def _extract_attempt(local_video_path: str, seek_s: float) -> _PosterAttempt:
    """Run FFmpeg once: scale + signalstats in one filter chain.

    Pipes JPEG to stdout; signalstats writes YAVG/YDEV to stderr. Raises
    PosterExtractionError on any failure mode (exit code, empty output,
    corrupt JPEG, missing stats).
    """
    # signalstats runs against the full-size frame; scale follows so the
    # output JPEG is downsampled but the stats reflect the original content.
    cmd = [
        "ffmpeg",
        "-ss",
        f"{seek_s:.3f}",
        "-i",
        local_video_path,
        "-frames:v",
        "1",
        "-vf",
        f"signalstats,scale={POSTER_WIDTH}:-1",
        "-q:v",
        str(POSTER_QUALITY),
        "-f",
        "mjpeg",
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
            f"ffmpeg timed out after {FFMPEG_TIMEOUT_S}s at seek={seek_s}s"
        ) from exc
    if result.returncode != 0 or not result.stdout:
        stderr = result.stderr.decode("utf-8", errors="replace")[:500]
        raise PosterExtractionError(
            f"ffmpeg returned {result.returncode} at seek={seek_s}s, stderr={stderr!r}"
        )
    if not result.stdout.startswith(b"\xff\xd8\xff"):
        raise PosterExtractionError(
            f"ffmpeg output is not a valid JPEG at seek={seek_s}s (got {len(result.stdout)} bytes)"
        )

    stderr_text = result.stderr.decode("utf-8", errors="replace")
    yavg_match = _YAVG_RE.search(stderr_text)
    ydev_match = _YDEV_RE.search(stderr_text)
    if yavg_match is None or ydev_match is None:
        raise PosterExtractionError(f"signalstats output missing YAVG/YDEV at seek={seek_s}s")

    return _PosterAttempt(
        seek_s=seek_s,
        jpeg=result.stdout,
        luma_mean=float(yavg_match.group(1)),
        luma_stddev=float(ydev_match.group(1)),
    )


def extract_poster_bytes(local_video_path: str) -> bytes:
    """Try each seek offset in POSTER_SEEK_ATTEMPTS_S, return the first frame
    that clears MIN_POSTER_LUMA + MIN_POSTER_VARIANCE.

    If every attempt fails the threshold (uniformly dark video, e.g. a night
    scene), return the brightest attempt's bytes — we never silently emit
    a black frame.

    Raises PosterExtractionError only if every attempt failed at the FFmpeg
    level (no valid JPEG produced at any seek).
    """
    attempts: list[_PosterAttempt] = []
    last_error: PosterExtractionError | None = None

    for seek_s in POSTER_SEEK_ATTEMPTS_S:
        try:
            attempt = _extract_attempt(local_video_path, seek_s)
        except PosterExtractionError as exc:
            # Skipping past end-of-video etc. — try the next offset.
            last_error = exc
            log.debug(
                "poster_attempt_failed",
                seek_s=seek_s,
                error=str(exc),
            )
            continue
        attempts.append(attempt)
        if attempt.passes_threshold():
            log.info(
                "poster_attempt_accepted",
                seek_s=seek_s,
                luma_mean=attempt.luma_mean,
                luma_stddev=attempt.luma_stddev,
                attempts_made=len(attempts),
            )
            return attempt.jpeg

    if not attempts:
        raise PosterExtractionError(
            f"all {len(POSTER_SEEK_ATTEMPTS_S)} seek attempts failed; last error: {last_error}"
        )

    # All attempts were too dark / low-variance — pick the brightest.
    brightest = max(attempts, key=lambda a: a.luma_mean)
    log.warning(
        "poster_all_dark",
        attempts=[
            {"seek_s": a.seek_s, "luma_mean": a.luma_mean, "luma_stddev": a.luma_stddev}
            for a in attempts
        ],
        selected_seek_s=brightest.seek_s,
    )
    return brightest.jpeg


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
