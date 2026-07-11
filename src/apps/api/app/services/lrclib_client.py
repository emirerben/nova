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
_API_GET_BY_ID_URL_TEMPLATE = "https://lrclib.net/api/get/{lrclib_id}"
_API_SEARCH_URL = "https://lrclib.net/api/search"
_TIMEOUT_S = 8.0
# Soft duration-mismatch penalties for the /api/search fuzzy fallback.
# Music-video uploads commonly carry 5-15s of spoken intro/outro that the
# LRCLIB recording does not, so a hard ±2s gate (as on /api/get) is too
# strict here. Score penalty grows with delta but never excludes outright
# — the title+artist similarity hard gates do the heavy lifting.
_FUZZY_DURATION_DELTA_WARN_S = 15.0
_FUZZY_DURATION_DELTA_HARD_S = 60.0
# Hard gate: title token-set similarity must reach this before a /search
# candidate is even considered. Below this, the song is almost certainly
# a different track that shares a coincidental keyword.
_FUZZY_MIN_TITLE_SIM = 0.85
# Combined-score gate that promotes a /api/search top result to a real
# match worth re-fetching via /api/get/{id}. Below this the agent treats
# the search as "no strong match" and routes to needs_manual_lyrics.
_FUZZY_MIN_COMBINED_SCORE = 0.85

# Backoff schedule for HTTP 429. Total worst case ~6.5s before we surrender
# and the caller falls back to whisper-only. Stays well inside the
# `lyrics_extraction_timeout_s = 90s` agent budget.
_RETRY_DELAYS_S: tuple[float, ...] = (0.5, 1.5, 4.5)

# LRCLIB asks for a descriptive User-Agent in their docs. Costs nothing and
# helps them debug bad clients. Matches the format the Genius client uses.
_USER_AGENT = "Kria/1.0 (+https://usekria.com)"

# Match one [mm:ss(.xxx)?] timestamp. We apply this REPEATEDLY at the start
# of each LRC line to handle the common chorus shorthand
# "[00:12.00][01:12.00][02:30.50]Never gonna give you up", where one text
# line carries three different start times.
_TIMESTAMP_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")

# Metadata tags LRC files put at the top: [ar:Artist], [ti:Title],
# [length:03:21]. Distinguishable from timestamps by an alpha prefix
# (timestamps start with a digit). We skip these.
_METADATA_TAG_RE = re.compile(r"^\[[a-zA-Z][a-zA-Z0-9_-]*:[^\]]*\]\s*$")


def search_lrclib(
    title: str,
    artist: str = "",
    *,
    duration_s: float | None = None,
) -> LrclibLyrics:
    """Look up lyrics on LRCLIB.

    Args:
        title: Cleaned track title. Pre-clean with
            `app.services.lyrics_search_query.build_lyrics_search_query`
            before calling — LRCLIB matches strictly on artist_name + track_name.
        artist: Cleaned artist name. May be empty; LRCLIB will use title-only
            matching but the hit quality drops sharply.
        duration_s: Track duration in seconds. When supplied, LRCLIB only
            returns a row whose recording length is within ±2s (LRCLIB's own
            tolerance). Critical for songs that exist in multiple recordings
            (radio edit, remix, extended) where lyric text matches but
            line-level timestamps differ — without it LRCLIB returns any
            matching title+artist row and the synced anchors land at the
            wrong absolute times (see PR #356/#358 incident on Hawai). Pass
            None or 0 to skip duration disambiguation.

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
    if duration_s is not None and duration_s > 0:
        # LRCLIB accepts integer seconds with ±2s match tolerance.
        params["duration"] = str(int(round(duration_s)))

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


# ── /api/get/{id} — direct lookup by LRCLIB row ID ────────────────────────────


def get_lrclib_by_id(lrclib_id: int) -> LrclibLyrics:
    """Fetch an LRCLIB row by its exact numeric ID.

    Used by the admin manual-override path: an admin who knows the correct
    row (e.g. found it on lrclib.net) pastes the ID or URL and Kria
    re-extracts against that specific row, bypassing the title-search step.

    Single shot, same headers + retry behavior as `search_lrclib`. The
    LRCLIB endpoint is `GET https://lrclib.net/api/get/{id}` (path-param
    form, no query params). The response body shape is identical to
    `/api/get?track_name=...&artist_name=...`.

    Args:
        lrclib_id: Positive integer LRCLIB row ID. Caller is responsible
            for validation (use `app.services.lrclib_id_parse.parse_lrclib_id`
            to extract from admin input).

    Raises:
        LrclibNotFound: HTTP 404 (row ID doesn't exist), or 200 with empty
            plainLyrics+syncedLyrics+instrumental=False.
        LrclibError: HTTP 5xx, network error, malformed JSON, or 429 after
            exhausting all retries.
        ValueError: lrclib_id is not a positive integer.
    """
    if not isinstance(lrclib_id, int) or lrclib_id <= 0:
        raise ValueError(f"lrclib_id must be a positive integer, got {lrclib_id!r}")

    url = _API_GET_BY_ID_URL_TEMPLATE.format(lrclib_id=lrclib_id)
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

    try:
        with httpx.Client(timeout=_TIMEOUT_S, headers=headers) as client:
            resp = _get_with_retry(client, url, {})
    except httpx.HTTPError as exc:
        raise LrclibError(f"lrclib network error: {exc}") from exc

    if resp.status_code == 404:
        raise LrclibNotFound(f"lrclib has no row with id={lrclib_id}")
    if resp.status_code == 429:
        raise LrclibError("lrclib still rate-limited (429) after retries — falling back")
    if resp.status_code >= 400:
        raise LrclibError(f"lrclib returned {resp.status_code}: {resp.text[:200]}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise LrclibError(f"lrclib returned non-JSON: {exc}") from exc

    if not isinstance(body, dict):
        raise LrclibError(f"lrclib returned unexpected shape: {type(body).__name__}")

    if body.get("statusCode") == 404 or body.get("name") == "NotFoundError":
        raise LrclibNotFound(f"lrclib NotFoundError body for id={lrclib_id}")

    return _hydrate_lrclib_lyrics(body, fallback_title="", fallback_artist="")


# ── /api/search — fuzzy fallback when /api/get 404s ───────────────────────────


@dataclass(frozen=True, slots=True)
class LrclibSearchCandidate:
    """One row from `/api/search`, scored locally.

    The combined score is in [0.0, 1.0]; the agent's `_FUZZY_MIN_COMBINED_SCORE`
    is the promote-to-real-match threshold.
    """

    lrclib_id: int
    title: str  # LRCLIB-matched track_name
    artist: str  # LRCLIB-matched artist_name
    duration_s: float | None
    title_similarity: float  # 0.0-1.0 token-set similarity (HARD GATE upstream)
    # |candidate.duration - request.duration|; None if either unknown.
    duration_delta_s: float | None
    # Weighted blend: title*0.5 + artist_match*0.3 + duration_penalty*0.2.
    combined_score: float


def search_lrclib_fuzzy(
    title: str,
    artist: str = "",
    *,
    duration_s: float | None = None,
) -> list[LrclibSearchCandidate]:
    """Fuzzy fallback when `/api/get` returns 404.

    `/api/get` is exact-string indexed and returns 404 for any title with
    feature credits, accent variants, or apostrophe quirks LRCLIB doesn't
    normalize. `/api/search` is full-text and ranks results, so it's the
    natural second-chance path.

    Scoring:
      * title token-set similarity (HARD gate at `_FUZZY_MIN_TITLE_SIM`,
        weight 0.5 in combined score)
      * artist case-insensitive match after stripping `ft.`/`feat.` from
        either side (HARD gate: candidates failing the artist check are
        dropped entirely, no soft-penalty)
      * duration delta in seconds (SOFT signal, weight 0.2). Music-video
        audio uploads carry intro/outro that LRCLIB's recording doesn't,
        so deltas up to ~15s are normal. Penalty grows linearly to
        `_FUZZY_DURATION_DELTA_HARD_S` (60s); past that the candidate is
        dropped.

    Returns:
        Candidates sorted by `combined_score` descending. May be empty.
        Caller (agent) applies the final `_FUZZY_MIN_COMBINED_SCORE` gate
        before deciding to re-fetch by ID and align.

    Raises:
        LrclibError: HTTP 5xx, network error, malformed JSON, 429 retries
            exhausted.
        LrclibNotFound: empty title, or `/api/search` returns 0 candidates
            (treating 0-row response as not-found is consistent with the
            `/api/get` contract).
    """
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title:
        raise LrclibNotFound("empty title — nothing to search")

    params: dict[str, str] = {"track_name": title}
    if artist:
        params["artist_name"] = artist
    # Don't pass `duration` to /api/search — it's a HARD ±2s filter there too,
    # defeating the whole point of using /search as a relaxed fallback.

    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

    try:
        with httpx.Client(timeout=_TIMEOUT_S, headers=headers) as client:
            resp = _get_with_retry(client, _API_SEARCH_URL, params)
    except httpx.HTTPError as exc:
        raise LrclibError(f"lrclib network error: {exc}") from exc

    if resp.status_code == 404:
        raise LrclibNotFound(f"lrclib /api/search 404 for {title!r} / {artist!r}")
    if resp.status_code == 429:
        raise LrclibError("lrclib still rate-limited (429) after retries — falling back")
    if resp.status_code >= 400:
        raise LrclibError(f"lrclib returned {resp.status_code}: {resp.text[:200]}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise LrclibError(f"lrclib returned non-JSON: {exc}") from exc

    if not isinstance(body, list):
        raise LrclibError(f"lrclib /api/search returned unexpected shape: {type(body).__name__}")

    if not body:
        raise LrclibNotFound(f"lrclib /api/search empty result for {title!r} / {artist!r}")

    candidates: list[LrclibSearchCandidate] = []
    for row in body:
        if not isinstance(row, dict):
            continue
        scored = _score_search_candidate(
            row, request_title=title, request_artist=artist, request_duration_s=duration_s
        )
        if scored is None:
            continue
        candidates.append(scored)

    candidates.sort(key=lambda c: c.combined_score, reverse=True)

    log.info(
        "lrclib_fuzzy_search",
        title=title,
        artist=artist,
        duration_s=duration_s,
        candidate_count=len(candidates),
        top_score=candidates[0].combined_score if candidates else None,
        top_id=candidates[0].lrclib_id if candidates else None,
    )

    return candidates


def _score_search_candidate(
    row: dict,
    *,
    request_title: str,
    request_artist: str,
    request_duration_s: float | None,
) -> LrclibSearchCandidate | None:
    """Score one /api/search row against the request. Returns None for
    hard-gate failures (title similarity below the floor, or duration
    delta past the hard limit, or missing required fields).
    """
    try:
        lrclib_id = int(row.get("id") or 0)
    except (TypeError, ValueError):
        return None
    if lrclib_id <= 0:
        return None

    cand_title = (row.get("trackName") or "").strip()
    cand_artist = (row.get("artistName") or "").strip()
    if not cand_title:
        return None

    title_sim = _title_token_set_similarity(request_title, cand_title)
    if title_sim < _FUZZY_MIN_TITLE_SIM:
        return None

    # Artist gate — case-insensitive equal after stripping any trailing
    # "ft./feat./featuring …" from either side. Empty request artist matches
    # any candidate (admin uploaded a track with no artist field).
    if request_artist and not _artists_match(request_artist, cand_artist):
        return None
    artist_match_score = (
        1.0 if not request_artist else (1.0 if _artists_match(request_artist, cand_artist) else 0.0)
    )

    cand_duration_raw = row.get("duration")
    try:
        cand_duration_s: float | None = float(cand_duration_raw) if cand_duration_raw else None
    except (TypeError, ValueError):
        cand_duration_s = None

    duration_delta_s: float | None = None
    duration_score = 1.0  # No request duration → no penalty.
    if request_duration_s is not None and request_duration_s > 0 and cand_duration_s is not None:
        duration_delta_s = abs(cand_duration_s - request_duration_s)
        if duration_delta_s >= _FUZZY_DURATION_DELTA_HARD_S:
            # Hard limit: this is clearly a different recording (extended
            # mix vs single, or a totally different song that happens to
            # share a title token).
            return None
        # Linear penalty: 0s delta → 1.0, 15s delta → 0.0, beyond → already
        # rejected above. Allow 15-60s span to score below zero but the
        # combined score gate naturally filters those out anyway.
        if duration_delta_s <= _FUZZY_DURATION_DELTA_WARN_S:
            duration_score = 1.0 - (duration_delta_s / _FUZZY_DURATION_DELTA_WARN_S)
        else:
            # Soft tail: 15-60s span maps to [0.0, -1.0]; let the combined
            # score gate reject anything that ends up below `_FUZZY_MIN_COMBINED_SCORE`.
            duration_score = -1.0 * (
                (duration_delta_s - _FUZZY_DURATION_DELTA_WARN_S)
                / (_FUZZY_DURATION_DELTA_HARD_S - _FUZZY_DURATION_DELTA_WARN_S)
            )

    combined = 0.5 * title_sim + 0.3 * artist_match_score + 0.2 * duration_score

    return LrclibSearchCandidate(
        lrclib_id=lrclib_id,
        title=cand_title,
        artist=cand_artist,
        duration_s=cand_duration_s,
        title_similarity=round(title_sim, 4),
        duration_delta_s=round(duration_delta_s, 2) if duration_delta_s is not None else None,
        combined_score=round(combined, 4),
    )


def _title_token_set_similarity(a: str, b: str) -> float:
    """Token-set Jaccard similarity over lowercased word tokens.

    Robust to word reordering, punctuation differences, and feature credits
    that the sanitizer left in. Returns 1.0 for identical token sets,
    0.0 for disjoint, fractional in between.
    """
    tokens_a = _tokenize_title(a)
    tokens_b = _tokenize_title(b)
    if not tokens_a or not tokens_b:
        return 0.0
    inter = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(inter) / len(union)


_TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize_title(s: str) -> set[str]:
    """Lowercase tokens of [a-z0-9] runs. Drops punctuation, accents
    (after NFKD), and feature credits."""
    import unicodedata  # noqa: PLC0415 — only needed here

    folded = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return set(_TITLE_TOKEN_RE.findall(folded.lower()))


_ARTIST_TAIL_FEAT_RE = re.compile(r"\s+(?:ft|feat|featuring)\.?\s+.+$", flags=re.IGNORECASE)


def _artists_match(a: str, b: str) -> bool:
    """Case-insensitive artist equality after stripping trailing
    `ft./feat./featuring …` and surrounding whitespace from either side."""

    def _norm(s: str) -> str:
        s = _ARTIST_TAIL_FEAT_RE.sub("", s.strip())
        return s.casefold()

    return _norm(a) == _norm(b)


def _hydrate_lrclib_lyrics(
    body: dict,
    *,
    fallback_title: str,
    fallback_artist: str,
) -> LrclibLyrics:
    """Build LrclibLyrics from a `/api/get*` 200-response body.

    Shared between `search_lrclib` (title+artist lookup) and
    `get_lrclib_by_id` (ID lookup) — the response body shape is identical
    across both endpoints.

    Raises:
        LrclibNotFound: 200 body with empty plain+synced AND not instrumental
            (LRCLIB sometimes returns an empty row for very recent uploads).
    """
    instrumental = bool(body.get("instrumental"))
    matched_title = (body.get("trackName") or "").strip() or fallback_title
    matched_artist = (body.get("artistName") or "").strip() or fallback_artist
    lrclib_id_raw = body.get("id")
    try:
        lrclib_id = int(lrclib_id_raw) if lrclib_id_raw is not None else 0
    except (TypeError, ValueError):
        lrclib_id = 0

    plain_lyrics = body.get("plainLyrics") or ""
    synced_lyrics = body.get("syncedLyrics") or ""
    plain_lines = _parse_plain_lyrics(plain_lyrics) if plain_lyrics else ()
    synced_lines = _parse_synced_lyrics(synced_lyrics) if synced_lyrics else None

    if not instrumental and not plain_lines and not synced_lines:
        raise LrclibNotFound(f"lrclib row had empty plainLyrics + syncedLyrics for id={lrclib_id}")

    log.info(
        "lrclib_lyrics_fetched_by_id" if not fallback_title else "lrclib_lyrics_fetched",
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
