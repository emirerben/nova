"""LRCLIB API client for fetching canonical lyric text + line-level timing.

LRCLIB (https://lrclib.net) is a free, key-less lyrics database that exposes
both plain text AND LRC-format synced lyrics (line-level timestamps). We use
it as the canonical text source — replacing Genius — and feed the synced
line bounds into the alignment layer as hard anchors for Whisper's per-word
timings (Whisper still provides word-grain because LRC is line-level only).

Single endpoint, single shot:
    GET https://lrclib.net/api/get?artist_name=<a>&track_name=<t>

No authentication. The service is community-funded; we set a descriptive
User-Agent per their request and retry 429s with jittered backoff so a
parallel Celery batch upload doesn't silently degrade legitimate tracks
to whisper-only.

This module does the HTTP + LRC parsing only. Title hygiene (stripping
"(Official Video)" / artist deduplication) lives in `lyrics_search_query`
and is shared with any other future lyric backend.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger()


class LrclibError(Exception):
    """Network failure, unexpected response shape, or exhausted retries."""


class LrclibNotFound(LrclibError):
    """LRCLIB has no entry for the supplied track/artist (HTTP 404 or empty body)."""


@dataclass(frozen=True, slots=True)
class SyncedLine:
    start_s: float
    text: str


@dataclass(frozen=True, slots=True)
class LrclibLyrics:
    title: str  # trackName as LRCLIB matched it
    artist: str  # artistName as LRCLIB matched it
    plain_lines: tuple[str, ...]  # parsed from plainLyrics; () when missing
    synced_lines: tuple[SyncedLine, ...] | None  # None when syncedLyrics is null
    instrumental: bool
    lrclib_id: int

    @property
    def full_text(self) -> str:
        """Joined plain-text body, useful as a Whisper prompt hint upstream."""
        return "\n".join(self.plain_lines)

    @property
    def synced_text(self) -> str:
        """Joined text from synced lines (no timestamps)."""
        if not self.synced_lines:
            return ""
        return "\n".join(line.text for line in self.synced_lines)


_API_URL = "https://lrclib.net/api/get"
_TIMEOUT_S = 8.0

# Backoff schedule for HTTP 429. Total worst case ~6.5s before we surrender
# and the caller falls back to whisper-only. Stays well inside the
# `lyrics_extraction_timeout_s = 90s` agent budget.
_RETRY_DELAYS_S: tuple[float, ...] = (0.5, 1.5, 4.5)

# LRCLIB asks for a descriptive User-Agent in their docs. Costs nothing and
# helps them debug bad clients. Matches the format the Genius client uses.
_USER_AGENT = "Nova/1.0 (+https://nova-video.vercel.app)"

# Match one [mm:ss(.xxx)?] timestamp. We apply this REPEATEDLY at the start
# of each LRC line to handle the common chorus shorthand
# "[00:12.00][01:12.00][02:30.50]Never gonna give you up", where one text
# line carries three different start times.
_TIMESTAMP_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")

# Metadata tags LRC files put at the top: [ar:Artist], [ti:Title],
# [length:03:21]. Distinguishable from timestamps by an alpha prefix
# (timestamps start with a digit). We skip these.
_METADATA_TAG_RE = re.compile(r"^\[[a-zA-Z][a-zA-Z0-9_-]*:[^\]]*\]\s*$")


def search_lrclib(title: str, artist: str = "") -> LrclibLyrics:
    """Look up lyrics on LRCLIB.

    Args:
        title: Cleaned track title. Pre-clean with
            `app.services.lyrics_search_query.build_lyrics_search_query`
            before calling — LRCLIB matches strictly on artist_name + track_name.
        artist: Cleaned artist name. May be empty; LRCLIB will use title-only
            matching but the hit quality drops sharply.

    Raises:
        LrclibNotFound: HTTP 404, or 200 with empty plainLyrics+syncedLyrics.
        LrclibError: HTTP 5xx, network error, malformed JSON, or 429 after
            exhausting all retries.
    """
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title:
        raise LrclibNotFound("empty title — nothing to search")

    params = {"track_name": title}
    if artist:
        params["artist_name"] = artist

    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

    try:
        with httpx.Client(timeout=_TIMEOUT_S, headers=headers) as client:
            resp = _get_with_retry(client, _API_URL, params)
    except httpx.HTTPError as exc:
        raise LrclibError(f"lrclib network error: {exc}") from exc

    if resp.status_code == 404:
        raise LrclibNotFound(f"lrclib has no entry for {title!r} / {artist!r}")
    if resp.status_code == 429:
        # Retries exhausted upstream.
        raise LrclibError("lrclib still rate-limited (429) after retries — falling back")
    if resp.status_code >= 400:
        raise LrclibError(f"lrclib returned {resp.status_code}: {resp.text[:200]}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise LrclibError(f"lrclib returned non-JSON: {exc}") from exc

    if not isinstance(body, dict):
        raise LrclibError(f"lrclib returned unexpected shape: {type(body).__name__}")

    # The 404 case is normally surfaced via HTTP status, but LRCLIB has been
    # observed to return 200 with a NotFoundError-shaped body on edge cases
    # (caching, regional CDN). Treat the documented error envelope as 404.
    if body.get("statusCode") == 404 or body.get("name") == "NotFoundError":
        raise LrclibNotFound(f"lrclib NotFoundError body for {title!r} / {artist!r}")

    instrumental = bool(body.get("instrumental"))
    matched_title = (body.get("trackName") or "").strip() or title
    matched_artist = (body.get("artistName") or "").strip() or artist
    lrclib_id_raw = body.get("id")
    try:
        lrclib_id = int(lrclib_id_raw) if lrclib_id_raw is not None else 0
    except (TypeError, ValueError):
        lrclib_id = 0

    plain_lyrics = body.get("plainLyrics") or ""
    synced_lyrics = body.get("syncedLyrics") or ""

    plain_lines = _parse_plain_lyrics(plain_lyrics) if plain_lyrics else ()
    synced_lines = _parse_synced_lyrics(synced_lyrics) if synced_lyrics else None

    # Empty-body guard — LRCLIB occasionally returns a 200 row with both
    # plainLyrics and syncedLyrics blank for very recent uploads.
    # Instrumental tracks are a legitimate "found but no lyrics" case and
    # are NOT treated as not-found here.
    if not instrumental and not plain_lines and not synced_lines:
        raise LrclibNotFound(
            f"lrclib row had empty plainLyrics + syncedLyrics for {title!r} / {artist!r}"
        )

    log.info(
        "lrclib_lyrics_fetched",
        title=matched_title,
        artist=matched_artist,
        lrclib_id=lrclib_id,
        instrumental=instrumental,
        plain_line_count=len(plain_lines),
        synced_line_count=len(synced_lines) if synced_lines else 0,
    )

    return LrclibLyrics(
        title=matched_title,
        artist=matched_artist,
        plain_lines=plain_lines,
        synced_lines=synced_lines,
        instrumental=instrumental,
        lrclib_id=lrclib_id,
    )


def _get_with_retry(client: httpx.Client, url: str, params: dict[str, str]) -> httpx.Response:
    """GET with automatic retry on HTTP 429.

    LRCLIB is keyless; rate limits are IP-scoped and shared across our
    Celery worker pool. During admin batch uploads we routinely fire 10+
    track analyses in parallel, which trips the limit. Without retry every
    track that was unlucky enough to hit a 429 silently degrades to
    whisper-only — which kills karaoke quality for no real reason.

    Up to 3 retries with jittered exponential backoff (0.5, 1.5, 4.5s).
    Honors a `Retry-After` header if present; jitter ±20% to prevent the
    worker pool from re-colliding at the same instant.

    5xx is NOT retried — different failure mode (service broken vs.
    throttled) and faster to degrade than to wait.
    """
    # The trailing `None` marker means "no more sleeps — return the last
    # 429 response and let the caller raise LrclibError."
    for base_delay in (*_RETRY_DELAYS_S, None):
        resp = client.get(url, params=params)
        if resp.status_code != 429:
            return resp
        if base_delay is None:
            return resp

        retry_after_raw = resp.headers.get("Retry-After")
        delay: float
        if retry_after_raw is not None:
            try:
                delay = float(retry_after_raw)
            except ValueError:
                # Some servers send an HTTP-date; we don't parse those —
                # fall back to our computed delay.
                delay = base_delay
        else:
            delay = base_delay
        delay *= random.uniform(0.8, 1.2)

        log.info(
            "lrclib_rate_limited_retrying",
            attempt=_RETRY_DELAYS_S.index(base_delay) + 1,
            delay_s=round(delay, 2),
            retry_after_header=retry_after_raw,
        )
        time.sleep(delay)

    # Unreachable: the `None` sentinel above always returns first.
    raise LrclibError("lrclib retry loop fell through")  # pragma: no cover


def _parse_plain_lyrics(text: str) -> tuple[str, ...]:
    """Split plain LRCLIB lyrics into a tuple of non-empty lines.

    LRCLIB's plainLyrics field is just newline-separated text with no
    timestamps and no section markers in most rows. Strip BOM, normalize
    line endings, drop blank lines.
    """
    if not text:
        return ()
    if text.startswith("﻿"):
        text = text[1:]
    lines = [line.strip() for line in text.splitlines()]
    return tuple(line for line in lines if line)


def _parse_synced_lyrics(text: str) -> tuple[SyncedLine, ...] | None:
    """Parse LRC-format synced lyrics into a sorted tuple of SyncedLines.

    Handles:
      - Standard `[mm:ss.xx]text` and `[mm:ss.xxx]text`
      - Multi-timestamp lines: `[00:12.00][01:12.00]Chorus` → 2 SyncedLines
      - Metadata tags `[ar:Artist]`, `[ti:Title]`, `[length:03:21]` (skipped)
      - Blank-text lines `[01:23.45]` with nothing after (skipped — usually
        marks an instrumental break)
      - BOM at file start
      - Hundredths OR thousandths in the fractional seconds component

    Returns None when the input is empty or yields zero valid lines —
    callers treat None as "no synced lyrics available" and fall back to
    the plain-text path.
    """
    if not text:
        return None
    if text.startswith("﻿"):
        text = text[1:]

    out: list[SyncedLine] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _METADATA_TAG_RE.match(line):
            continue

        # Pull off all leading timestamps. `match.end()` walks the cursor
        # forward over each `[mm:ss.xx]` token; whatever's left is the
        # text body shared by every timestamp on this line.
        timestamps: list[float] = []
        cursor = 0
        while True:
            m = _TIMESTAMP_RE.match(line, cursor)
            if m is None:
                break
            minutes = int(m.group(1))
            seconds = int(m.group(2))
            frac_raw = m.group(3) or "0"
            # Treat `.5` as 500ms (left-aligned), not 5ms — LRC convention.
            # Pad to 3 digits so .5 → 500, .50 → 500, .500 → 500.
            frac_ms = int(frac_raw.ljust(3, "0")[:3])
            start_s = minutes * 60 + seconds + frac_ms / 1000.0
            timestamps.append(start_s)
            cursor = m.end()

        if not timestamps:
            # Line started with `[` but didn't match a timestamp and wasn't
            # caught by the metadata regex (e.g. `[Verse 1]`). Skip —
            # section markers are noise for alignment.
            continue

        body = line[cursor:].strip()
        if not body:
            # `[01:23.45]` with no text → instrumental break or formatting
            # quirk. Skip; the alignment layer will interpolate.
            continue

        for ts in timestamps:
            out.append(SyncedLine(start_s=ts, text=body))

    if not out:
        return None

    out.sort(key=lambda sl: sl.start_s)
    return tuple(out)
