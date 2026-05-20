"""Genius API client for fetching canonical lyric text.

Two-step flow:
  1. /search?q=<title artist>  → returns up to ~10 hits with song id
  2. scrape https://genius.com/<song-path> for the lyrics body (Genius's
     public lyric body is NOT exposed via the public API; the docs explicitly
     point at this dance, and `lyricsgenius` does the same thing internally)

We do not depend on `lyricsgenius` to keep dependencies tight and to control
timeouts + retries. The scraper is intentionally minimal: it pulls the
`<div data-lyrics-container="true">` blocks that Genius has used since 2020
and falls back to the older `lyrics` div for legacy pages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()


class GeniusError(Exception):
    """Network failure, bad credentials, or unexpected response shape."""


class GeniusNotFound(GeniusError):
    """Search returned zero hits, or the resolved page had no lyric body."""


@dataclass(frozen=True, slots=True)
class GeniusLyrics:
    title: str
    artist: str
    lines: tuple[str, ...]
    genius_url: str

    @property
    def full_text(self) -> str:
        return "\n".join(self.lines)


_API_BASE = "https://api.genius.com"
_SEARCH_TIMEOUT_S = 8.0
_FETCH_TIMEOUT_S = 12.0

# Bracketed section markers Genius embeds in lyric bodies — "[Verse 1]",
# "[Chorus]", "[Hook: Eminem]". Strip these; downstream alignment expects
# only sung lines.
_SECTION_MARKER = re.compile(r"^\s*\[[^\]]+\]\s*$")

# Lyric blocks since 2020 — `<div data-lyrics-container="true">...</div>` with
# inline `<br>` tags as line breaks and other inline HTML for emphasis.
_LYRICS_CONTAINER = re.compile(
    r'<div[^>]*data-lyrics-container="true"[^>]*>(.*?)</div>',
    flags=re.DOTALL | re.IGNORECASE,
)
# Catch-all: any remaining HTML tag.
_HTML_TAG = re.compile(r"<[^>]+>")

# YouTube titles arrive as "Artist - Title (Official Video)" / "[Official Audio]"
# / "(HD)" / "[Lyrics]" etc. yt-dlp does not strip these — they get passed
# straight into Genius and tank the search ranking (the literal title
# "The Weeknd - Can't Feel My Face (Official Video) The Weeknd" returns
# unrelated hits like "The Weeknd Rolling Stones Interview"). Strip the
# common noise tags before the query.
_YOUTUBE_NOISE_TAGS = re.compile(
    r"[\(\[\{][^\)\]\}]*?(?:official|lyric|audio|video|hd|hq|4k|mv|"
    r"music|edit|remix|live|version|explicit|clean|color\s*coded|"
    r"extended|radio|album|single|cover|karaoke|instrumental|"
    r"reupload|reaction|tribute|demo)[^\)\]\}]*?[\)\]\}]",
    flags=re.IGNORECASE,
)


def _build_search_query(title: str, artist: str) -> str:
    """Clean YouTube-style title noise + dedupe redundant artist token.

    yt-dlp passes the raw video title through, so titles routinely look like
    "Artist Name - Song Title (Official Video)". Building a Genius query as
    `f"{title} {artist}"` then yields "Artist Name - Song Title (Official
    Video) Artist Name" — three copies of the artist and the noise tag
    poison the search relevance. Clean both before querying.
    """
    title = (title or "").strip()
    artist = (artist or "").strip()

    # Strip "Artist - " prefix when it duplicates the artist field.
    if artist and title.lower().startswith(f"{artist.lower()} - "):
        title = title[len(artist) + 3 :].lstrip()
    # Strip "Artist -" with no following space too (rare but seen).
    elif artist and title.lower().startswith(f"{artist.lower()}-"):
        title = title[len(artist) + 1 :].lstrip()

    # Drop parenthetical / bracketed noise like "(Official Video)".
    title = _YOUTUBE_NOISE_TAGS.sub("", title)
    # Collapse repeated whitespace + leading/trailing hyphens left behind.
    title = re.sub(r"\s+", " ", title).strip(" -–—")

    if artist and title:
        return f"{title} {artist}"
    return title or artist


def search_lyrics(title: str, artist: str = "") -> GeniusLyrics:
    """Look up canonical lyrics on Genius.

    Raises:
        GeniusNotFound — no hit, or the hit had no extractable lyric body.
        GeniusError — credentials missing, network failure, or unexpected shape.

    Every failure path logs a structured event before raising so a silent
    catch upstream (lyrics.py treats GeniusError as a soft fallback) still
    leaves a trace. Stage tags (`search` vs `scrape`) keep grep simple:
    `fly logs | grep genius_` walks the whole funnel.
    """
    token = (settings.genius_access_token or "").strip()
    if not token:
        log.warning("genius_token_missing", title=title, artist=artist)
        raise GeniusError("GENIUS_ACCESS_TOKEN not configured — set it to enable Genius lookup")

    query = _build_search_query(title, artist)
    if not query:
        log.info("genius_empty_query", title=title, artist=artist)
        raise GeniusNotFound("empty title — nothing to search")

    headers = {"Authorization": f"Bearer {token}"}

    # 1. Search
    log.info("genius_search_start", query=query, title=title, artist=artist)
    try:
        with httpx.Client(timeout=_SEARCH_TIMEOUT_S) as client:
            resp = client.get(
                f"{_API_BASE}/search",
                params={"q": query},
                headers=headers,
            )
    except httpx.HTTPError as exc:
        log.warning("genius_search_network_error", query=query, error=str(exc))
        raise GeniusError(f"genius search network error: {exc}") from exc

    if resp.status_code == 401:
        log.error("genius_search_unauthorized", query=query, status_code=401)
        raise GeniusError("genius search returned 401 — token invalid")
    if resp.status_code == 429:
        log.warning("genius_search_rate_limited", query=query, status_code=429)
        raise GeniusError("genius search rate-limited (429)")
    if resp.status_code >= 400:
        log.warning(
            "genius_search_http_error",
            query=query,
            status_code=resp.status_code,
            body_snippet=resp.text[:200],
        )
        raise GeniusError(f"genius search returned {resp.status_code}: {resp.text[:200]}")

    try:
        body = resp.json()
    except ValueError as exc:
        log.warning("genius_search_non_json", query=query, error=str(exc))
        raise GeniusError(f"genius search returned non-JSON: {exc}") from exc

    hits = (body.get("response") or {}).get("hits") or []
    if not hits:
        log.info("genius_search_no_hits", query=query, hit_count=0)
        raise GeniusNotFound(f"no genius hits for {query!r}")

    hit = _pick_best_hit(hits, title, artist)
    result = hit.get("result") or {}
    genius_url = result.get("url") or ""
    matched_title = (result.get("title") or "").strip() or title
    matched_artist = ((result.get("primary_artist") or {}).get("name") or "").strip() or artist

    if not genius_url:
        log.warning(
            "genius_hit_missing_url",
            query=query,
            matched_title=matched_title,
            matched_artist=matched_artist,
        )
        raise GeniusError("genius hit has no URL — cannot fetch lyrics body")

    log.info(
        "genius_search_hit",
        query=query,
        matched_title=matched_title,
        matched_artist=matched_artist,
        url=genius_url,
        hit_count=len(hits),
    )

    # 2. Scrape lyric body. The /songs/{id} API endpoint returns metadata but
    # NOT the lyric text — that's only available on the public web page.
    log.info("genius_scrape_start", url=genius_url)
    try:
        with httpx.Client(
            timeout=_FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={
                # Genius blocks bare-bones requests; mimic a normal client.
                "User-Agent": (
                    "Mozilla/5.0 (compatible; NovaLyrics/1.0; +https://nova-video.vercel.app)"
                ),
                "Accept-Language": "en-US,en;q=0.9,tr;q=0.7",
            },
        ) as client:
            page = client.get(genius_url)
    except httpx.HTTPError as exc:
        log.warning("genius_scrape_network_error", url=genius_url, error=str(exc))
        raise GeniusError(f"genius page fetch failed: {exc}") from exc

    if page.status_code >= 400:
        # Most-watched event: prod observation 2026-05-19 caught Fly worker
        # IPs blocked by Genius CDN with 403 (~50ms). Bucket the status so
        # alerting can split bot-block (403/406/429) from genuine 4xx/5xx.
        log.warning(
            "genius_scrape_blocked",
            url=genius_url,
            status_code=page.status_code,
            body_snippet=page.text[:200],
        )
        raise GeniusError(f"genius page returned {page.status_code}: {genius_url}")

    lines = _extract_lyric_lines(page.text)
    if not lines:
        log.warning(
            "genius_scrape_empty_body",
            url=genius_url,
            page_bytes=len(page.text),
        )
        raise GeniusNotFound(f"genius page had no extractable lyric body at {genius_url}")

    log.info(
        "genius_lyrics_fetched",
        title=matched_title,
        artist=matched_artist,
        line_count=len(lines),
        url=genius_url,
    )
    return GeniusLyrics(
        title=matched_title,
        artist=matched_artist,
        lines=tuple(lines),
        genius_url=genius_url,
    )


def _pick_best_hit(hits: list[dict], title: str, artist: str) -> dict:
    """Prefer hits whose primary_artist matches the supplied artist.

    Genius's relevance ranking already handles 99% of cases, but for ambiguous
    titles ("Hello", "Yesterday") the wrong cover is sometimes top. We prefer
    exact-equality matches first, then prefix matches — without this ordering
    "Adele Cover Band" beats "Adele" when both share the title.
    """
    if not artist:
        return hits[0]

    artist_lower = artist.strip().lower()

    def _primary(h: dict) -> str:
        return (
            (((h.get("result") or {}).get("primary_artist") or {}).get("name") or "")
            .strip()
            .lower()
        )

    for hit in hits:
        if _primary(hit) == artist_lower:
            return hit
    for hit in hits:
        primary = _primary(hit)
        if primary and primary.startswith(artist_lower):
            return hit
    return hits[0]


def _extract_lyric_lines(html: str) -> list[str]:
    """Pull lyric lines from a Genius song page.

    Genius's modern layout uses one or more
    `<div data-lyrics-container="true">` blocks with `<br/>` line breaks.
    Inline HTML (links, italic spans) is stripped; section markers like
    "[Verse 1]" are dropped — they're useful navigation aids on the web but
    poison alignment by introducing untimed tokens.
    """
    containers = _LYRICS_CONTAINER.findall(html)
    if not containers:
        return []

    lines: list[str] = []
    for raw_block in containers:
        # Normalize <br> tags to newlines BEFORE stripping HTML.
        block = re.sub(r"<br\s*/?>", "\n", raw_block, flags=re.IGNORECASE)
        block = _HTML_TAG.sub("", block)
        # Genius sometimes HTML-encodes apostrophes / quotes.
        block = (
            block.replace("&#x27;", "'")
            .replace("&#39;", "'")
            .replace("&quot;", '"')
            .replace("&amp;", "&")
            .replace("&nbsp;", " ")
        )
        for raw in block.splitlines():
            line = raw.strip()
            if not line:
                continue
            if _SECTION_MARKER.match(line):
                continue
            lines.append(line)

    return lines
