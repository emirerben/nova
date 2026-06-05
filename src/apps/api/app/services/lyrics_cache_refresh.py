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
from datetime import UTC, datetime

import structlog

from app.agents._runtime import RunContext
from app.agents.lyrics import (
    PUBLISHABLE_LYRICS_SOURCES,
    LyricsExtractionAgent,
    LyricsInput,
)
from app.config import settings
from app.database import sync_session as _sync_session
from app.models import MusicTrack
from app.storage import download_to_file

log = structlog.get_logger()


class LyricsCacheRefreshError(RuntimeError):
    """Raised when a stale cache cannot be refreshed for a lyrics-enabled render."""


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
    """

    cfg = lyrics_config or {}
    if not cfg.get("enabled"):
        return lyrics_cached
    if not lyrics_cache_is_stale(lyrics_cached):
        return lyrics_cached
    if not settings.openai_api_key:
        raise LyricsCacheRefreshError(
            "lyrics_cached is stale and OPENAI_API_KEY is missing; refusing stale lyric render"
        )

    target_version = current_lyrics_prompt_version()
    old_version = lyrics_cached.get("prompt_version") if isinstance(lyrics_cached, dict) else None
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
        output = LyricsExtractionAgent(model_client=None).run(  # type: ignore[arg-type]
            LyricsInput(
                audio_path=local_audio,
                track_title=title,
                artist=artist,
                best_start_s=float(track_config.get("best_start_s", 0.0) or 0.0),
                best_end_s=float(track_config.get("best_end_s", 0.0) or 0.0),
                duration_s=duration_s,
                forced_lrclib_id=forced_lrclib_id,
            ),
            ctx=RunContext(job_id=f"track:{track_id}:render-refresh"),
        )

    if output.is_empty or output.source not in PUBLISHABLE_LYRICS_SOURCES:
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
