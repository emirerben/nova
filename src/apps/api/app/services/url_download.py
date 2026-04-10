"""Download videos from TikTok (and other platforms) via yt-dlp, then upload to GCS."""

import re
import tempfile
import uuid
from pathlib import Path

import structlog
import yt_dlp

from app.config import settings
from app.storage import _get_client

log = structlog.get_logger()

# Patterns that yt-dlp can handle (TikTok, Instagram Reels, YouTube Shorts, etc.)
_SUPPORTED_URL_RE = re.compile(
    r"https?://(www\.)?(tiktok\.com|vm\.tiktok\.com|instagram\.com|youtube\.com|youtu\.be)/",
    re.IGNORECASE,
)

MAX_DURATION_S = 180  # Reject videos longer than 3 minutes (templates are short-form)


class DownloadError(Exception):
    """Raised when yt-dlp fails to download the video."""


def is_supported_url(url: str) -> bool:
    return bool(_SUPPORTED_URL_RE.match(url.strip()))


def download_and_upload(url: str) -> str:
    """Download a video from *url* via yt-dlp and upload to GCS.

    Returns the GCS object path (e.g. ``templates/<uuid>/video.mp4``).

    Raises ``DownloadError`` on any failure (network, unsupported URL, too long, etc.).
    """
    url = url.strip()
    template_upload_id = str(uuid.uuid4())
    gcs_path = f"templates/{template_upload_id}/video.mp4"

    with tempfile.TemporaryDirectory(prefix="nova_dl_") as tmpdir:
        out_path = str(Path(tmpdir) / "video.mp4")

        ydl_opts: dict = {
            "outtmpl": out_path,
            "format": "mp4/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # Reject very long videos early via match_filter
            "match_filter": yt_dlp.utils.match_filter_func(
                f"duration <= {MAX_DURATION_S}"
            ),
        }

        log.info("url_download_start", url=url, gcs_path=gcs_path)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as exc:
            log.warning("url_download_failed", url=url, error=str(exc))
            raise DownloadError(f"Failed to download video: {exc}") from exc
        except Exception as exc:
            log.warning("url_download_unexpected", url=url, error=str(exc))
            raise DownloadError(f"Unexpected download error: {exc}") from exc

        # yt-dlp may produce a slightly different filename; find the actual file
        downloaded = _find_video(tmpdir)
        if downloaded is None:
            raise DownloadError("Download succeeded but no video file was produced")

        # Upload to GCS
        bucket = _get_client().bucket(settings.storage_bucket)
        blob = bucket.blob(gcs_path)
        blob.upload_from_filename(str(downloaded), content_type="video/mp4")

        log.info("url_download_uploaded", gcs_path=gcs_path, size_mb=round(downloaded.stat().st_size / 1e6, 1))

    return gcs_path


def _find_video(directory: str) -> Path | None:
    """Find the first video file in *directory* (yt-dlp may rename the output)."""
    for ext in ("mp4", "mkv", "webm", "mov"):
        files = list(Path(directory).glob(f"*.{ext}"))
        if files:
            return files[0]
    return None
