"""Render-time guard for stale lyric caches.

Music renders read `MusicTrack.lyrics_cached`, so a deployed alignment fix is
only visible after that JSONB blob is regenerated. This module makes that
freshness requirement explicit at render time: when lyrics are enabled and the
cache predates the live LyricsExtractionAgent prompt_version, refresh it before
the renderer can consume stale timings.
"""

from __future__ import annotations

import os
import tempfile
import time
from datetime import UTC, datetime

import structlog

from app.agents._runtime import RunContext
from app.agents.lyrics import (
    PUBLISHABLE_LYRICS_SOURCES,
    LyricsExtractionAgent,
    LyricsInput,
    LyricsOutput,
)
from app.config import settings
from app.database import sync_session as _sync_session
from app.models import MusicTrack
from app.storage import download_to_file

log = structlog.get_logger()

# LRCLIB (the published-lyrics provider) occasionally returns a transient
# transport error mid-refresh. A short retry almost always recovers, which is
# what was failing the `song_lyrics` variant in prod after the 2026-06-06
# prompt bump made the whole library's cache stale at once.
_LRCLIB_REFRESH_ATTEMPTS = 3
_LRCLIB_REFRESH_BACKOFF_S = (1.0, 3.0)


def _sleep(seconds: float) -> None:
    """Backoff indirection so tests can patch out the real sleep."""
    time.sleep(seconds)


class LyricsCacheRefreshError(RuntimeError):
    """Raised when a stale cache cannot be refreshed for a lyrics-enabled render."""


class TransientLyricsCacheRefreshError(LyricsCacheRefreshError):
    """Raised when an external lyrics provider failed during a stale-cache refresh."""


def current_lyrics_prompt_version() -> str:
    return LyricsExtractionAgent.spec.prompt_version


def lyrics_cache_is_stale(lyrics_cached: dict | None) -> bool:
    if not isinstance(lyrics_cached, dict):
        return False
    return lyrics_cached.get("prompt_version") != current_lyrics_prompt_version()


def ensure_fresh_lyrics_cached_for_render(
    *,
    track_id: str,
    lyrics_cached: dict | None,
    lyrics_config: dict | None,
    reason: str,
) -> dict | None:
    """Return a current lyrics cache for a render, refreshing stale rows.

    If lyrics are disabled, missing, or already current, this is a no-op. If
    lyrics are enabled and the cache is stale, the function synchronously
    re-runs lyric extraction, persists the fresh publishable result, and returns
    it. Failure raises so the caller cannot silently burn stale timings.

    The synchronous refresh is a fallback. The offline `backfill_stale_lyrics`
    task (run after any LyricsExtractionAgent prompt bump) is what should keep
    the library fresh so renders never pay this download+ASR+LRCLIB tax. When the
    refresh does run inline, transient LRCLIB hiccups are retried before the
    variant is allowed to fail.
    """

    cfg = lyrics_config or {}
    if not cfg.get("enabled"):
        return lyrics_cached
    if not lyrics_cache_is_stale(lyrics_cached):
        return lyrics_cached

    old_version = lyrics_cached.get("prompt_version") if isinstance(lyrics_cached, dict) else None
    return refresh_track_lyrics_cache(track_id=track_id, reason=reason, old_version=old_version)


def refresh_track_lyrics_cache(
    *,
    track_id: str,
    reason: str,
    old_version: str | None = None,
) -> dict:
    """Regenerate `MusicTrack.lyrics_cached` at the live prompt version, now.

    Unconditional: the caller decides *whether* a refresh is needed (the
    render-time guard above gates on enabled+stale; the offline backfill gates
    on staleness). Downloads the track audio, re-runs the LyricsExtractionAgent
    (retrying transient LRCLIB failures), persists the fresh publishable result,
    and returns it. Raises `TransientLyricsCacheRefreshError` (retryable) or
    `LyricsCacheRefreshError` (terminal) on failure.
    """
    if not settings.openai_api_key:
        raise LyricsCacheRefreshError(
            "lyrics_cached is stale and OPENAI_API_KEY is missing; refusing stale lyric render"
        )

    target_version = current_lyrics_prompt_version()
    log.warning(
        "lyrics_cache_stale_refresh_start",
        track_id=track_id,
        reason=reason,
        old_prompt_version=old_version,
        target_prompt_version=target_version,
    )

    with _sync_session() as db:
        track = db.get(MusicTrack, track_id)
        if track is None:
            raise LyricsCacheRefreshError(f"MusicTrack {track_id} not found")
        if not track.audio_gcs_path:
            raise LyricsCacheRefreshError(f"MusicTrack {track_id} has no audio_gcs_path")
        audio_gcs_path = track.audio_gcs_path
        track_config = track.track_config or {}
        title = (track.title or "").strip()
        artist = (track.artist or "").strip()
        duration_s = float(track.duration_s or 0.0)

    forced_lrclib_id = _forced_lrclib_id(track_config)
    with tempfile.TemporaryDirectory(prefix="nova_lyrics_render_refresh_") as tmpdir:
        local_audio = os.path.join(tmpdir, "audio.m4a")
        download_to_file(audio_gcs_path, local_audio)
        output = _extract_lyrics_with_lrclib_retry(
            LyricsInput(
                audio_path=local_audio,
                track_title=title,
                artist=artist,
                best_start_s=float(track_config.get("best_start_s", 0.0) or 0.0),
                best_end_s=float(track_config.get("best_end_s", 0.0) or 0.0),
                duration_s=duration_s,
                forced_lrclib_id=forced_lrclib_id,
            ),
            track_id=track_id,
            reason=reason,
        )

    if output.is_empty or output.source not in PUBLISHABLE_LYRICS_SOURCES:
        if _non_publishable_due_to_lrclib_error(output):
            log.warning(
                "lyrics_cache_stale_refresh_transient_lrclib_error",
                track_id=track_id,
                reason=reason,
                source=output.source,
                lrclib_error=(output.lyrics_diagnostic or {}).get("lrclib_error"),
            )
            raise TransientLyricsCacheRefreshError(
                "LRCLIB lookup failed while refreshing stale cached lyrics; retry the render"
            )
        _persist_non_publishable_refresh(track_id=track_id, output=output)
        raise LyricsCacheRefreshError(
            "stale lyrics_cached refresh did not produce publishable LRCLIB lyrics"
        )

    fresh = output.model_dump()
    if fresh.get("prompt_version") != target_version:
        raise LyricsCacheRefreshError(
            "stale lyrics_cached refresh returned unexpected prompt_version"
        )

    with _sync_session() as db:
        track = db.get(MusicTrack, track_id)
        if track is None:
            raise LyricsCacheRefreshError(f"MusicTrack {track_id} disappeared during refresh")
        track.lyrics_status = "ready"
        track.lyrics_cached = fresh
        track.lyrics_whisper_draft = None
        track.lyrics_source = output.source
        track.lyrics_error_detail = None
        track.lyrics_diagnostic = output.lyrics_diagnostic
        track.lyrics_extracted_at = datetime.now(UTC)
        db.commit()

    log.info(
        "lyrics_cache_stale_refresh_done",
        track_id=track_id,
        reason=reason,
        target_prompt_version=target_version,
        source=output.source,
        lines=len(output.lines),
    )
    return fresh


def _extract_lyrics_with_lrclib_retry(
    lyrics_input: LyricsInput,
    *,
    track_id: str,
    reason: str,
) -> LyricsOutput:
    """Run lyric extraction, retrying ONLY transient LRCLIB transport failures.

    A clean "no synced lyrics exist" result is terminal and must NOT be retried;
    only the LRCLIB-error case (network/timeout) gets another attempt. The audio
    is already local, so each retry re-hits the network, not GCS.
    """
    last: LyricsOutput | None = None
    for attempt in range(1, _LRCLIB_REFRESH_ATTEMPTS + 1):
        output = LyricsExtractionAgent(model_client=None).run(  # type: ignore[arg-type]
            lyrics_input,
            ctx=RunContext(job_id=f"track:{track_id}:render-refresh"),
        )
        last = output
        publishable = not output.is_empty and output.source in PUBLISHABLE_LYRICS_SOURCES
        if publishable or not _non_publishable_due_to_lrclib_error(output):
            return output
        if attempt < _LRCLIB_REFRESH_ATTEMPTS:
            idx = min(attempt - 1, len(_LRCLIB_REFRESH_BACKOFF_S) - 1)
            backoff = _LRCLIB_REFRESH_BACKOFF_S[idx]
            log.warning(
                "lyrics_cache_refresh_lrclib_retry",
                track_id=track_id,
                reason=reason,
                attempt=attempt,
                max_attempts=_LRCLIB_REFRESH_ATTEMPTS,
                backoff_s=backoff,
            )
            _sleep(backoff)
    assert last is not None  # loop body runs at least once
    return last


def _non_publishable_due_to_lrclib_error(output: LyricsOutput) -> bool:
    """Return true when a non-publishable output came from LRCLIB transport failure."""

    diagnostic = output.lyrics_diagnostic
    if not isinstance(diagnostic, dict):
        return False
    if not diagnostic.get("lrclib_error"):
        return False

    statuses = (
        diagnostic.get("get_status"),
        diagnostic.get("search_status"),
    )
    return any(isinstance(status, str) and "error" in status for status in statuses)


def _persist_non_publishable_refresh(*, track_id: str, output: LyricsOutput) -> None:
    """Move a failed render-time refresh into the normal manual-recovery state."""

    with _sync_session() as db:
        track = db.get(MusicTrack, track_id)
        if track is None:
            return
        track.lyrics_status = "needs_manual_lyrics"
        track.lyrics_cached = None
        track.lyrics_whisper_draft = output.model_dump()
        track.lyrics_source = output.source
        track.lyrics_error_detail = "LRCLIB lookup failed; paste a row ID to recover"
        track.lyrics_diagnostic = output.lyrics_diagnostic
        track.lyrics_extracted_at = datetime.now(UTC)
        db.commit()

    log.warning(
        "lyrics_cache_stale_refresh_needs_manual",
        track_id=track_id,
        source=output.source,
        lines=len(output.lines),
    )


def _forced_lrclib_id(track_config: dict) -> int | None:
    lyrics_cfg = track_config.get("lyrics_config") or {}
    if not isinstance(lyrics_cfg, dict):
        return None
    raw = lyrics_cfg.get("forced_lrclib_id")
    if raw is None:
        return None
    try:
        forced_id = int(raw)
    except (TypeError, ValueError):
        return None
    return forced_id if forced_id > 0 else None
