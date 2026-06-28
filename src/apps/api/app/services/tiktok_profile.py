"""TikTok public-profile scraper for user onboarding.

Lifted from scripts/research/fetch_tiktok.py (standalone research script).
This version is importable by Celery tasks and app routes.

Best-effort: private accounts, rate limits, and invalid handles all return None
rather than raising. The caller falls back to the no-TikTok interview path.

Two fetch modes:
- fetch_profile() — flat extract, ~10s, captions + hashtags only. Used by the
  pre-screen + chat interviewer. DO NOT CHANGE its behavior or limit — the pre-
  screen UX depends on the ~10s response time.
- fetch_profile_enriched() — full per-video extract (1 yt-dlp request/video),
  adds view/like/comment counts + engagement_rate + view_index. Used by the
  background analyze_tiktok_profile task (soft_limit=210s).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TypedDict

import structlog

log = structlog.get_logger()

_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)
_DEFAULT_LIMIT = 30
_ENRICH_LIMIT = 30
_SCRAPE_TIMEOUT_S = 10


class TikTokProfile(TypedDict):
    handle: str
    follower_count: int | None
    video_count: int
    top_captions: list[str]
    top_hashtags: list[str]
    analyzed_at: str


class TikTokVideoRecord(TypedDict):
    """Per-video record from the enriched fetch.

    Mirrors the shape mined by scripts/research/fetch_tiktok.py so the
    PerformanceSignal schema in market_research.py maps to it directly.
    """

    caption: str
    hashtags: list[str]
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    repost_count: int | None
    # (likes + comments + reposts) / views — None when views are missing or when
    # only flat-extract counts are available (no enrich).
    engagement_rate: float | None
    # views / account-median-views — outperformance vs the account's own baseline.
    # None until median_views is computed across all fetched videos.
    view_index: float | None
    duration: int | None
    upload_date: str | None  # YYYYMMDD when available
    # Video identity — optional so existing records are backward-compatible.
    # Populated by the enriched fetch (_to_video_record); absent from flat fetch.
    video_id: str | None
    webpage_url: str | None  # e.g. https://www.tiktok.com/@handle/video/<id>


class TikTokProfileEnriched(TypedDict):
    """Richer profile from the full per-video extract (one yt-dlp request/video)."""

    handle: str
    follower_count: int | None
    video_count: int
    median_views: float | None
    # Sorted view-desc so the analyzer sees top performers first.
    videos: list[TikTokVideoRecord]
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


# ---------------------------------------------------------------------------
# Helpers for the enriched fetch — lifted verbatim from
# scripts/research/fetch_tiktok.py. DO NOT import that script (it is
# standalone with no app.* imports by design).
# ---------------------------------------------------------------------------


def _engagement_rate_calc(
    views: object, likes: object, comments: object, reposts: object
) -> float | None:
    """(likes + comments + reposts) / views — account-size independent.

    None when views are missing/zero or no engagement counts are present.
    Mirrors PerformanceSignal.engagement_rate.
    """
    if not isinstance(views, (int, float)) or views <= 0:
        return None
    parts = [c for c in (likes, comments, reposts) if isinstance(c, (int, float))]
    if not parts:
        return None
    return round(sum(parts) / views, 6)


def _median_views(values: list[float]) -> float | None:
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def _to_video_record(entry: dict) -> TikTokVideoRecord:
    """Normalize a yt-dlp entry to a TikTokVideoRecord."""
    caption = entry.get("description") or entry.get("title") or ""
    hashtags = _HASHTAG_RE.findall(caption)
    views = entry.get("view_count")
    likes = entry.get("like_count")
    comments = entry.get("comment_count")
    reposts = entry.get("repost_count")
    return TikTokVideoRecord(
        caption=caption,
        hashtags=hashtags,
        view_count=views,
        like_count=likes,
        comment_count=comments,
        repost_count=reposts,
        engagement_rate=_engagement_rate_calc(views, likes, comments, reposts),
        view_index=None,  # filled in after median is computed
        duration=entry.get("duration"),
        upload_date=entry.get("upload_date"),
        video_id=entry.get("id"),
        webpage_url=entry.get("webpage_url"),
    )


def fetch_profile_enriched(
    handle: str, *, limit: int = _ENRICH_LIMIT
) -> TikTokProfileEnriched | None:
    """Fetch enriched per-video TikTok metadata for the deep-analysis task.

    Unlike fetch_profile() (flat extract, ~10s), this does a full per-video
    extract (1 yt-dlp request/video). At limit=30 videos with socket_timeout=10
    the realistic wall-clock is 50–150s — budget under the Celery task's
    soft_time_limit=210.

    Returns None on any failure. Never raises — the caller is a best-effort task
    that falls back gracefully when the profile is unavailable.

    DO NOT use this from the pre-screen / chat-interview path — that path uses
    fetch_profile() and must stay ~10s.
    """
    try:
        import yt_dlp  # noqa: PLC0415
    except ImportError:
        log.warning("tiktok_enrich_skip", reason="yt-dlp not installed")
        return None

    from app.services.yt_dlp_options import with_yt_dlp_cookiefile  # noqa: PLC0415

    clean = normalize_handle(handle)
    if not clean:
        return None

    url = f"https://www.tiktok.com/@{clean}"
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,
        "ignoreerrors": True,
        "extract_flat": False,  # full per-video extract for like/comment/upload_date
        "playlistend": max(1, limit),
        "socket_timeout": _SCRAPE_TIMEOUT_S,
    }

    try:
        with with_yt_dlp_cookiefile() as cookie_file:
            if cookie_file is not None:
                opts["cookiefile"] = str(cookie_file.path)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("tiktok_enrich_failed", handle=clean, error=str(exc)[:200])
        return None

    if not info:
        return None

    entries = [e for e in (info.get("entries") or []) if e][:limit]

    videos: list[TikTokVideoRecord] = []
    for entry in entries:
        try:
            videos.append(_to_video_record(entry))
        except Exception as exc:  # noqa: BLE001
            log.warning("tiktok_enrich_video_skip", handle=clean, error=str(exc)[:200])

    # Compute view_index = views / account-median-views (account-size independent).
    view_counts = [v["view_count"] for v in videos if isinstance(v["view_count"], (int, float))]
    median = _median_views(view_counts)
    if median and median > 0:
        for v in videos:
            if isinstance(v["view_count"], (int, float)) and v["view_count"]:
                v["view_index"] = round(v["view_count"] / median, 4)

    # Sort view-desc so the analyzer sees top performers first.
    videos.sort(key=lambda v: v["view_count"] or 0, reverse=True)

    return TikTokProfileEnriched(
        handle=clean,
        follower_count=info.get("channel_follower_count") or info.get("follower_count"),
        video_count=len(videos),
        median_views=median,
        videos=videos,
        analyzed_at=datetime.now(UTC).isoformat(),
    )
