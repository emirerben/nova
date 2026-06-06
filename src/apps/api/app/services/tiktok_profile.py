"""TikTok public-profile scraper for user onboarding.

Lifted from scripts/research/fetch_tiktok.py (standalone research script).
This version is importable by Celery tasks and app routes.

Best-effort: private accounts, rate limits, and invalid handles all return None
rather than raising. The caller falls back to the no-TikTok interview path.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TypedDict

import structlog

log = structlog.get_logger()

_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)
_DEFAULT_LIMIT = 30
_SCRAPE_TIMEOUT_S = 10


class TikTokProfile(TypedDict):
    handle: str
    follower_count: int | None
    video_count: int
    top_captions: list[str]
    top_hashtags: list[str]
    analyzed_at: str


def normalize_handle(raw: str) -> str:
    """Strip leading @ and surrounding whitespace; tolerate full URLs."""
    h = raw.strip().lstrip("@")
    if "tiktok.com/@" in h:
        h = h.split("tiktok.com/@", 1)[1].split("/", 1)[0].split("?", 1)[0]
    return h


def fetch_profile(handle: str, *, limit: int = _DEFAULT_LIMIT) -> TikTokProfile | None:
    """Fetch public TikTok profile metadata for a given handle.

    Returns None on any failure (private account, invalid handle, rate limit,
    yt-dlp unavailable). Never raises — callers fall back to no-TikTok path.

    Cookies: reads YTDLP_COOKIES_PATH env var via yt_dlp_options.py convention.
    """
    try:
        import yt_dlp  # noqa: PLC0415 — optional dep not in prod image base
    except ImportError:
        log.warning("tiktok_scrape_skip", reason="yt-dlp not installed")
        return None

    from app.services.yt_dlp_options import with_yt_dlp_cookiefile  # noqa: PLC0415

    clean = normalize_handle(handle)
    if not clean:
        return None

    url = f"https://www.tiktok.com/@{clean}"
    base_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,
        "ignoreerrors": True,
        "extract_flat": True,
        "playlistend": max(1, limit),
        "socket_timeout": _SCRAPE_TIMEOUT_S,
    }

    try:
        with with_yt_dlp_cookiefile() as cookie_file:
            opts = dict(base_opts)
            if cookie_file is not None:
                opts["cookiefile"] = str(cookie_file.path)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning("tiktok_scrape_failed", handle=clean, error=str(exc)[:200])
        return None

    if not info:
        return None

    entries = [e for e in (info.get("entries") or []) if e][:limit]

    top_captions: list[str] = []
    all_hashtags: list[str] = []
    for entry in entries:
        caption = entry.get("description") or entry.get("title") or ""
        if caption:
            top_captions.append(caption[:300])
        all_hashtags.extend(_HASHTAG_RE.findall(caption))

    # Top-10 unique hashtags by first occurrence
    seen: set[str] = set()
    unique_hashtags: list[str] = []
    for tag in all_hashtags:
        if tag.lower() not in seen:
            seen.add(tag.lower())
            unique_hashtags.append(tag)
        if len(unique_hashtags) >= 10:
            break

    return TikTokProfile(
        handle=clean,
        follower_count=info.get("channel_follower_count") or info.get("follower_count"),
        video_count=len(entries),
        top_captions=top_captions[:10],
        top_hashtags=unique_hashtags,
        analyzed_at=datetime.now(UTC).isoformat(),
    )
