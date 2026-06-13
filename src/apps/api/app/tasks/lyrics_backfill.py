"""Offline backfill: regenerate stale lyric caches across the whole library.

A `LyricsExtractionAgent` prompt_version bump invalidates EVERY track's
`lyrics_cached` at once (staleness is keyed on prompt_version). Until each row
is regenerated, every lyrics-enabled render pays a synchronous
download + Whisper + LRCLIB tax in `ensure_fresh_lyrics_cached_for_render` — and
hard-fails the `song_lyrics` variant when LRCLIB hiccups (this is what was
failing prod jobs after the 2026-06-06 bump).

This task pre-warms the cache so renders read fresh rows and never refresh
inline. Run it once after any prompt bump:

    fly ssh console -a nova-video -C \
      "python -c 'from app.tasks.lyrics_backfill import backfill_stale_lyrics; \
       print(backfill_stale_lyrics.apply().result)'"

or enqueue it: `backfill_stale_lyrics.delay()`.
"""

from __future__ import annotations

import time

import structlog
from sqlalchemy import select

from app.database import sync_session
from app.models import MusicTrack
from app.services.lyrics_cache_refresh import (
    LyricsCacheRefreshError,
    TransientLyricsCacheRefreshError,
    lyrics_cache_is_stale,
    refresh_track_lyrics_cache,
)
from app.worker import celery_app

log = structlog.get_logger()

# Be polite to LRCLIB between tracks — the curated library is small (dozens of
# tracks), so sequential + a short gap avoids hammering the provider.
_INTER_TRACK_SLEEP_S = 1.0


def _sleep(seconds: float) -> None:
    """Indirection so tests can patch out the inter-track pause."""
    time.sleep(seconds)


def _stale_track_ids() -> list[str]:
    """IDs of every track whose cached lyrics predate the live prompt version."""
    with sync_session() as db:
        rows = db.execute(
            select(MusicTrack.id, MusicTrack.lyrics_cached).where(
                MusicTrack.lyrics_cached.isnot(None)
            )
        ).all()
    return [str(tid) for tid, cached in rows if lyrics_cache_is_stale(cached)]


@celery_app.task(
    name="tasks.backfill_stale_lyrics",
    bind=True,
    # Same envelope as render orchestrators: stay strictly under the broker
    # visibility_timeout (1900s) — see tests/tasks/test_task_time_limits.py.
    soft_time_limit=1740,
    time_limit=1800,
)
def backfill_stale_lyrics(self, *, limit: int | None = None, dry_run: bool = False) -> dict:
    """Regenerate every stale `lyrics_cached` row. Returns a summary dict.

    Best-effort: one bad track never aborts the sweep. `transient` counts
    tracks that failed on LRCLIB transport (worth re-running the task later);
    `failed` counts terminal failures (e.g. no synced lyrics exist).
    """
    stale = _stale_track_ids()
    if limit is not None:
        stale = stale[:limit]

    summary = {
        "candidates": len(stale),
        "refreshed": 0,
        "transient": 0,
        "failed": 0,
        "dry_run": dry_run,
    }
    log.info("lyrics_backfill_start", **summary)
    if dry_run:
        return summary

    for tid in stale:
        try:
            refresh_track_lyrics_cache(track_id=tid, reason="backfill")
            summary["refreshed"] += 1
        except TransientLyricsCacheRefreshError:
            summary["transient"] += 1
            log.warning("lyrics_backfill_transient", track_id=tid)
        except LyricsCacheRefreshError as exc:
            summary["failed"] += 1
            log.warning("lyrics_backfill_failed", track_id=tid, error=str(exc))
        except Exception:  # noqa: BLE001 — one bad track must not abort the sweep
            summary["failed"] += 1
            log.warning("lyrics_backfill_unexpected", track_id=tid, exc_info=True)
        _sleep(_INTER_TRACK_SLEEP_S)

    log.info("lyrics_backfill_done", **summary)
    return summary
