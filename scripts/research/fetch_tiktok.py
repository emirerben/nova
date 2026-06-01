#!/usr/bin/env python3
"""Fetch public TikTok account metadata for Nova's weekly market research.

This is the DETERMINISTIC, FREE first stage of the research pipeline (see
.claude/skills/research-tiktok/SKILL.md). It pulls captions + engagement
metadata via yt-dlp — NO video download, NO LLM, NO paid API. The Claude Code
analyst then reads the raw JSON this writes and mines it into the versioned
artifact banks (prompts/persona_archetypes.json, content_ideas.json,
overlay_examples.json).

Design notes:
- Standalone on purpose: yt-dlp + stdlib only, no `app.*` imports, so it runs
  in any worktree that has yt-dlp without booting FastAPI settings.
- Best-effort: TikTok is hostile to scraping. A failed account or video is
  logged and skipped; the batch never hard-fails on one bad fetch (mirrors the
  generative pipeline's "best-effort by design" posture).
- Cookie support matches the app convention: `--cookies <path>` or the
  `YTDLP_COOKIES_PATH` env var (same name app/services/yt_dlp_options.py reads).

Usage:
    python scripts/research/fetch_tiktok.py --accounts research/tiktok/accounts.txt
    python scripts/research/fetch_tiktok.py --handle nermozdemir --limit 5 --dry-run
    python scripts/research/fetch_tiktok.py --accounts ... --enrich   # +like/comment counts
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

try:
    import yt_dlp
except ImportError:  # pragma: no cover - import guard for a clearer error
    sys.stderr.write(
        "yt-dlp is required. Install it in your venv: pip install 'yt-dlp[default]>=2024.12'\n"
    )
    raise

_DEFAULT_LIMIT = 30
_DEFAULT_OUT = Path("research/tiktok/raw")
_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)


def _log(msg: str) -> None:
    sys.stderr.write(f"[fetch_tiktok] {msg}\n")
    sys.stderr.flush()


def _normalize_handle(raw: str) -> str:
    """Strip leading @ and surrounding whitespace; tolerate full URLs."""
    h = raw.strip().lstrip("@")
    if "tiktok.com/@" in h:
        h = h.split("tiktok.com/@", 1)[1].split("/", 1)[0].split("?", 1)[0]
    return h


def _read_accounts(path: Path) -> list[str]:
    handles: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        handles.append(_normalize_handle(line))
    return handles


def _base_opts(cookies: str | None) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,
        "ignoreerrors": True,  # one bad entry shouldn't abort the listing
    }
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def _hashtags(text: str) -> list[str]:
    return _HASHTAG_RE.findall(text or "")


def _engagement_rate(views, likes, comments, reposts) -> float | None:
    """(likes + comments + reposts) / views — account-size independent.

    None when views are missing/zero or no engagement counts are present (the
    flat extract returns view_count but not likes/comments, so this stays None
    unless --enrich was used). Mirrors PerformanceSignal.engagement_rate.
    """
    if not views or views <= 0:
        return None
    parts = [c for c in (likes, comments, reposts) if isinstance(c, (int, float))]
    if not parts:
        return None
    return round(sum(parts) / views, 6)


def _record(entry: dict) -> dict:
    """Normalize a yt-dlp entry to the fields the analyst mines."""
    caption = entry.get("description") or entry.get("title") or ""
    views = entry.get("view_count")
    likes = entry.get("like_count")
    comments = entry.get("comment_count")
    reposts = entry.get("repost_count")
    return {
        "id": str(entry.get("id") or ""),
        "url": entry.get("url") or entry.get("webpage_url") or "",
        "caption": caption,
        "hashtags": _hashtags(caption),
        "view_count": views,
        "like_count": likes,
        "comment_count": comments,
        "repost_count": reposts,
        # Pre-computed so the analyst mines a grounded PerformanceSignal instead of
        # eyeballing raw counts. view_index is filled in _fetch_account (needs the
        # account median across all videos); engagement_rate is per-video here.
        "engagement_rate": _engagement_rate(views, likes, comments, reposts),
        "view_index": None,
        "duration": entry.get("duration"),
        "upload_date": entry.get("upload_date"),  # YYYYMMDD when available
    }


def _median(values: list[float]) -> float | None:
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def _fetch_account(handle: str, limit: int, enrich: bool, cookies: str | None) -> dict:
    url = f"https://www.tiktok.com/@{handle}"
    opts = _base_opts(cookies)
    # Flat extract lists the account cheaply (one request); rich per-video
    # fields (like/comment counts, upload_date) only come from a full extract.
    opts["extract_flat"] = not enrich
    opts["playlistend"] = max(1, limit)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = [e for e in (info.get("entries") or []) if e]

    videos: list[dict] = []
    for entry in entries[:limit]:
        try:
            videos.append(_record(entry))
        except Exception as exc:  # noqa: BLE001 - best-effort per video
            _log(f"  skip video {entry.get('id')}: {type(exc).__name__}: {exc}")

    # view_index = views / account-median views — outperformance vs the account's
    # own baseline (account-size independent). Computed across all fetched videos.
    median_views = _median([v["view_count"] for v in videos if v.get("view_count")])
    if median_views and median_views > 0:
        for v in videos:
            if v.get("view_count"):
                v["view_index"] = round(v["view_count"] / median_views, 4)

    # Engagement-desc so the analyst sees top performers first.
    videos.sort(key=lambda v: v.get("view_count") or 0, reverse=True)
    return {
        "handle": handle,
        "account_url": url,
        "fetched_at": datetime.now(UTC).isoformat(),
        "follower_count": info.get("channel_follower_count") or info.get("follower_count"),
        "video_count": len(videos),
        "videos": videos,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--accounts", type=Path, help="File with one handle per line.")
    src.add_argument("--handle", type=str, help="A single handle (with or without @).")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT, help="Max videos per account.")
    parser.add_argument(
        "--out", type=Path, default=_DEFAULT_OUT, help="Output directory for raw JSON."
    )
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="Full per-video extract (adds like/comment counts, upload_date); slower.",
    )
    parser.add_argument(
        "--cookies",
        type=str,
        default=os.environ.get("YTDLP_COOKIES_PATH") or None,
        help="Netscape cookie file (defaults to $YTDLP_COOKIES_PATH).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Fetch + print summary, do not write files."
    )
    args = parser.parse_args()

    if args.accounts:
        if not args.accounts.exists():
            _log(f"accounts file not found: {args.accounts}")
            return 1
        handles = _read_accounts(args.accounts)
    else:
        handles = [_normalize_handle(args.handle)]

    if not handles:
        _log("no handles to fetch")
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%d")
    if not args.dry_run:
        args.out.mkdir(parents=True, exist_ok=True)

    ok = 0
    for handle in handles:
        _log(f"fetching @{handle} (limit={args.limit}, enrich={args.enrich})...")
        try:
            data = _fetch_account(handle, args.limit, args.enrich, args.cookies)
        except Exception as exc:  # noqa: BLE001 - best-effort per account
            _log(f"  FAILED @{handle}: {type(exc).__name__}: {exc}")
            continue
        ok += 1
        _log(f"  got {data['video_count']} videos")
        if args.dry_run:
            top = data["videos"][:3]
            print(
                json.dumps(
                    {"handle": handle, "video_count": data["video_count"], "top": top},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            continue
        out_path = args.out / f"{handle}-{stamp}.json"
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _log(f"  wrote {out_path}")

    _log(f"done: {ok}/{len(handles)} accounts fetched")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
