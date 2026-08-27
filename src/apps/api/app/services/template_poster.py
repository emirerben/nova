"""Extract source-matched poster JPEGs from browser-visible videos.

Each seek attempt uses one FFmpeg subprocess (NEVER MoviePy / VideoFileClip —
see CLAUDE.md); extraction tries up to six offsets within a 90-second budget.
The template helper writes to ``templates/<id>/poster.jpg``.  Job-output
helpers write a deterministic ``<video-object>.poster.jpg`` sibling and return
that relative key so callers can persist it without storing a signed URL.

The first 1.5s of some templates is a fade-in, so the historical fixed-seek
strategy produced near-black thumbnails. We now try a small sequence of seek
offsets and pick the first frame whose mean luma and luma stddev clear minimum
thresholds — if all fall short, we return the brightest attempt so we never
silently emit a black frame.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass

import structlog

from app.storage import download_to_file, upload_bytes_public_read

log = structlog.get_logger()

# Ordered list of seek offsets the extractor will try until one clears the
# brightness/variance thresholds. The first entry is the historical default
# (1.5s); the rest are escape hatches for fade-in intros.
POSTER_SEEK_ATTEMPTS_S: tuple[float, ...] = (1.5, 0.1, 0.5, 3.0, 5.0, 10.0)
# Mean luma (Y channel, 0-255). YAVG below this looks visually black/very dark.
MIN_POSTER_LUMA = 35.0
# Stddev of luma — low values indicate a uniform-color frame (likely a fade
# midpoint or a solid-color hold) even when the mean isn't pitch-black.
MIN_POSTER_VARIANCE = 8.0
POSTER_WIDTH = 540
POSTER_QUALITY = 4  # ffmpeg -q:v scale: 1=best, 31=worst. 4 ≈ 30-60KB at 540px.
FFMPEG_TIMEOUT_S = 30
# A single poster must not turn a worker render into six serial 30-second waits.
POSTER_TOTAL_BUDGET_S = 90

# ``showinfo`` emits the luma mean/stddev as ``mean:[...] stdev:[...]``. Keep
# accepting the historical signalstats YAVG/YDEV form too so older FFmpeg
# output and existing callers remain compatible.
_SHOWINFO_STATS_RE = re.compile(r"mean:\[\s*([\d.]+)[^]]*\]\s+stdev:\[\s*([\d.]+)")
_YAVG_RE = re.compile(r"YAVG\s*[:=]\s*([\d.]+)")
_YDEV_RE = re.compile(r"YDEV\s*[:=]\s*([\d.]+)")


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


def _extract_attempt(
    local_video_path: str,
    seek_s: float,
    *,
    timeout_s: float = FFMPEG_TIMEOUT_S,
) -> _PosterAttempt:
    """Run FFmpeg once: scale + signalstats in one filter chain.

    Pipes JPEG to stdout; ``showinfo`` writes luma mean/stddev to stderr. Raises
    PosterExtractionError on any failure mode (exit code, empty output,
    corrupt JPEG, missing stats).
    """
    # showinfo/signalstats run against the full-size frame; scale follows so
    # the output JPEG is downsampled but the stats reflect the source frame.
    cmd = [
        "ffmpeg",
        "-ss",
        f"{seek_s:.3f}",
        "-i",
        local_video_path,
        "-frames:v",
        "1",
        "-vf",
        # showinfo prints mean/stdev in stock FFmpeg. signalstats remains in the
        # chain for compatibility with older builds that expose YAVG/YDEV.
        f"signalstats,showinfo,scale={POSTER_WIDTH}:-1",
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
            timeout=timeout_s,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise PosterExtractionError(
            f"ffmpeg timed out after {timeout_s:.3f}s at seek={seek_s}s"
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
    showinfo_match = _SHOWINFO_STATS_RE.search(stderr_text)
    yavg_match = _YAVG_RE.search(stderr_text)
    ydev_match = _YDEV_RE.search(stderr_text)
    if showinfo_match is not None:
        luma_mean = float(showinfo_match.group(1))
        luma_stddev = float(showinfo_match.group(2))
    elif yavg_match is not None and ydev_match is not None:
        luma_mean = float(yavg_match.group(1))
        luma_stddev = float(ydev_match.group(1))
    else:
        raise PosterExtractionError(f"signalstats output missing YAVG/YDEV at seek={seek_s}s")

    return _PosterAttempt(
        seek_s=seek_s,
        jpeg=result.stdout,
        luma_mean=luma_mean,
        luma_stddev=luma_stddev,
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
    started_at = time.monotonic()

    for seek_s in POSTER_SEEK_ATTEMPTS_S:
        if time.monotonic() - started_at >= POSTER_TOTAL_BUDGET_S:
            last_error = PosterExtractionError(
                f"poster extraction exceeded {POSTER_TOTAL_BUDGET_S}s total budget"
            )
            break
        remaining_s = POSTER_TOTAL_BUDGET_S - (time.monotonic() - started_at)
        if remaining_s <= 0:
            last_error = PosterExtractionError(
                f"poster extraction exceeded {POSTER_TOTAL_BUDGET_S}s total budget"
            )
            break
        try:
            attempt = _extract_attempt(
                local_video_path,
                seek_s,
                timeout_s=min(FFMPEG_TIMEOUT_S, remaining_s),
            )
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


def poster_object_path(video_object_path: str) -> str:
    """Return the deterministic JPEG sibling for a playable video object."""
    return f"{video_object_path}.poster.jpg"


def upload_video_poster(local_video_path: str, video_object_path: str) -> str:
    """Extract and upload a poster next to a browser-visible video object."""
    if not os.path.isfile(local_video_path):
        raise PosterExtractionError(f"video source does not exist: {local_video_path}")
    poster_path = poster_object_path(video_object_path)
    poster_bytes = extract_poster_bytes(local_video_path)
    upload_bytes_public_read(poster_bytes, poster_path, content_type="image/jpeg")
    log.info(
        "video_poster_published",
        video_path=video_object_path,
        poster_path=poster_path,
        bytes=len(poster_bytes),
    )
    return poster_path


def generate_and_upload_from_gcs(
    video_object_path: str,
    *,
    job_id: str | None = None,
    source_kind: str = "video",
) -> str | None:
    """Best-effort poster generation for a video already uploaded to GCS.

    Terminal writers often upload the MP4 before their row-locked publication.
    Downloading that exact object here keeps poster generation centralized while
    allowing a healthy video to remain ready when FFmpeg or GCS has a transient
    failure.
    """
    if not isinstance(video_object_path, str) or not video_object_path.strip():
        return None
    with tempfile.TemporaryDirectory(prefix="nova_video_poster_") as tmpdir:
        local_path = f"{tmpdir}/source.mp4"
        try:
            download_to_file(video_object_path, local_path)
            return upload_video_poster(local_path, video_object_path)
        except Exception as exc:  # noqa: BLE001 - poster is fail-open by contract
            log.warning(
                "video_poster_extract_failed",
                job_id=job_id,
                source_kind=source_kind,
                video_path=video_object_path,
                error_class=type(exc).__name__,
                error=str(exc)[:300],
            )
            return None


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
