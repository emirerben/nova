"""Download audio tracks from YouTube / SoundCloud via yt-dlp and upload to GCS.

Unlike url_download.py (which handles short-form video templates), this module:
  - Targets audio-only extraction (m4a/AAC)
  - Supports longer durations (up to 10 min — admin downloads full song)
  - Covers YouTube AND SoundCloud (public tracks only)
  - Parses yt-dlp error messages to surface geo/rate errors to the admin
"""

import re
import subprocess
import tempfile
import uuid
from pathlib import Path

import structlog
import yt_dlp

from app.config import settings
from app.storage import _get_client

log = structlog.get_logger()

# YouTube and SoundCloud (public tracks — authenticated SoundCloud is excluded)
_SUPPORTED_AUDIO_URL_RE = re.compile(
    r"https?://(www\.)?(youtube\.com|youtu\.be|soundcloud\.com)/",
    re.IGNORECASE,
)

# Admin downloads the full song; best section is extracted at render time
MAX_AUDIO_DURATION_S = 600  # 10 minutes

# yt-dlp error patterns for descriptive admin feedback
_GEO_PATTERN = re.compile(r"not available in your country|geo.?restrict", re.IGNORECASE)
_RATE_PATTERN = re.compile(r"429|too many requests|rate limit", re.IGNORECASE)
_UNAVAILABLE_PATTERN = re.compile(r"unavailable|private|removed|deleted", re.IGNORECASE)


class DownloadError(Exception):
    """Raised when audio download or upload fails. Message is user-facing (admin)."""


def is_supported_audio_url(url: str) -> bool:
    return bool(_SUPPORTED_AUDIO_URL_RE.match(url.strip()))


def download_audio_and_upload(url: str) -> tuple[str, float | None, str | None]:
    """Download audio from *url* and upload to GCS as m4a.

    Returns:
        (gcs_path, duration_s, thumbnail_url)
        - gcs_path: GCS object path, e.g. ``music/<uuid>/audio.m4a``
        - duration_s: track duration in seconds (None if not reported by yt-dlp)
        - thumbnail_url: remote thumbnail URL (None if unavailable)

    Raises:
        DownloadError: descriptive message for geo-blocks, rate limits, etc.
    """
    url = url.strip()
    track_id = str(uuid.uuid4())
    gcs_path = f"music/{track_id}/audio.m4a"

    with tempfile.TemporaryDirectory(prefix="nova_audio_dl_") as tmpdir:
        out_template = str(Path(tmpdir) / "audio.%(ext)s")

        ydl_opts: dict = {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                    "preferredquality": "192",
                }
            ],
            "outtmpl": out_template,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # Don't use match_filter for duration — yt-dlp's match_filter sometimes
            # rejects tracks whose duration metadata is missing. We enforce the limit
            # after download via ffprobe instead.
        }

        log.info("audio_download_start", url=url, gcs_path=gcs_path)

        info: dict | None = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as exc:
            _raise_descriptive_error(url, str(exc))
        except Exception as exc:
            log.warning("audio_download_unexpected", url=url, error=str(exc))
            raise DownloadError(f"Unexpected download error: {exc}") from exc

        # Find the produced audio file
        audio_file = _find_audio(tmpdir)
        if audio_file is None:
            raise DownloadError("Download succeeded but no audio file was produced")

        # Enforce duration limit via ffprobe
        duration_s = _probe_duration(str(audio_file))
        if duration_s is not None and duration_s > MAX_AUDIO_DURATION_S:
            raise DownloadError(
                f"Track is {duration_s:.0f}s — maximum allowed is {MAX_AUDIO_DURATION_S}s (10 min). "
                "Upload a shorter clip or trim to the best section first."
            )

        # Upload to GCS
        bucket = _get_client().bucket(settings.storage_bucket)
        blob = bucket.blob(gcs_path)
        blob.upload_from_filename(str(audio_file), content_type="audio/mp4")

        size_mb = round(audio_file.stat().st_size / 1e6, 1)
        log.info("audio_download_uploaded", gcs_path=gcs_path, size_mb=size_mb)

        thumbnail_url: str | None = None
        if info and isinstance(info, dict):
            thumbnail_url = info.get("thumbnail")

    return gcs_path, duration_s, thumbnail_url


def _raise_descriptive_error(url: str, raw_error: str) -> None:
    """Parse yt-dlp error text and raise a descriptive DownloadError."""
    if _GEO_PATTERN.search(raw_error):
        raise DownloadError(
            "This track is geo-restricted and cannot be downloaded from the server's region. "
            "Try a different YouTube/SoundCloud link."
        )
    if _RATE_PATTERN.search(raw_error):
        raise DownloadError(
            "YouTube/SoundCloud rate-limited the download. Wait a few minutes and try again."
        )
    if _UNAVAILABLE_PATTERN.search(raw_error):
        raise DownloadError(
            "This track is unavailable (private, removed, or deleted). "
            "Check the URL and try again."
        )
    # Generic fallback — include raw error for admin debugging
    raise DownloadError(f"Failed to download audio: {raw_error}")


def _find_audio(directory: str) -> Path | None:
    """Find the first audio file produced by yt-dlp in *directory*."""
    for ext in ("m4a", "mp4", "aac", "opus", "webm", "ogg"):
        files = list(Path(directory).glob(f"*.{ext}"))
        if files:
            return files[0]
    return None


def _probe_duration(audio_path: str) -> float | None:
    """Return duration in seconds via ffprobe; None on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        raw = result.stdout.strip()
        return float(raw) if raw else None
    except Exception:
        return None
