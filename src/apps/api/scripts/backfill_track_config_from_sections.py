"""Backfill MusicTrack.track_config + recipe_cached from rank-1 song_sections.

Usage (from src/apps/api/):
    python -m scripts.backfill_track_config_from_sections [--dry-run]
                                                          [--track-id UUID]
                                                          [--limit N]
                                                          [--include-stale]

For each MusicTrack where ``best_sections IS NOT NULL`` AND
``section_version == CURRENT_SECTION_VERSION``, rewrite
``track_config.best_start_s`` / ``best_end_s`` / ``required_clips_min`` /
``required_clips_max`` from rank-1, then regenerate ``recipe_cached``
against the new bounds (re-merging the cached visual layer).

Why this exists: ``analyze_music_track_task`` (post-fix) reconciles at
write time, but rows analyzed BEFORE that fix landed still hold the
legacy ``auto_best_section()`` 45s window AND a ``recipe_cached`` timed
against that 45s window. Templated music jobs render the cached recipe
verbatim, so without this backfill they keep using the legacy window
until each track is manually reanalyzed.

Idempotent: tracks where rank-1 already matches ``track_config`` are
skipped unless ``--include-stale`` is passed (for testing the helper
end-to-end on rows that are already correct).

Failures (cache refresh raises) are logged and the loop continues —
``track_config`` still gets the new bounds, so manual jobs (fresh recipe
gen) work; only templated music stays on stale cache for that track.
"""

from __future__ import annotations

import argparse
import sys

import structlog

from app.database import sync_session as _sync_session
from app.models import MusicTrack
from app.services.music_sections import (
    current_best_section_for_track,
    reconcile_track_config_to_rank_one,
    refresh_recipe_cached_for_bounds,
)

log = structlog.get_logger()


def _backfill_one(track: MusicTrack, *, include_stale: bool, dry_run: bool) -> str:
    """Return 'updated' | 'skipped_no_section' | 'skipped_already_current'
    | 'cache_refresh_failed' | 'skipped_no_change'.

    Mutates `track` in place when not dry_run. Caller commits.
    """
    bounds = current_best_section_for_track(track)
    if bounds is None:
        return "skipped_no_section"

    sec_start, sec_end = bounds
    cfg = dict(track.track_config or {})
    current_start = float(cfg.get("best_start_s") or 0.0)
    current_end = float(cfg.get("best_end_s") or 0.0)

    # Cheap idempotency check: skip when bounds already match rank-1 to
    # within rounding. `--include-stale` forces a rewrite for testing.
    matches = (
        abs(current_start - round(sec_start, 3)) < 0.01
        and abs(current_end - round(sec_end, 3)) < 0.01
    )
    if matches and not include_stale:
        return "skipped_already_current"

    beats = list(track.beat_timestamps_s or [])
    duration_s = float(track.duration_s or 0.0)

    new_config, source = reconcile_track_config_to_rank_one(
        track_config=cfg,
        beats=beats,
        sections=track.best_sections,
        section_version=track.section_version,
    )
    if source != "song_sections":
        # Section window produced 0 slots at current slot_every_n_beats.
        return "skipped_no_change"

    cache_status = "no_cache"
    new_recipe_cached = track.recipe_cached
    if track.recipe_cached is not None:
        try:
            new_recipe_cached = refresh_recipe_cached_for_bounds(
                recipe_cached=track.recipe_cached,
                beats=beats,
                track_config=new_config,
                duration_s=duration_s,
            )
            cache_status = "refreshed"
        except Exception as exc:
            log.warning(
                "backfill_recipe_cache_refresh_failed",
                track_id=track.id,
                error=str(exc),
            )
            cache_status = "refresh_failed"

    log.info(
        "backfill_apply",
        track_id=track.id,
        old_start=current_start,
        old_end=current_end,
        new_start=new_config["best_start_s"],
        new_end=new_config["best_end_s"],
        cache=cache_status,
        dry_run=dry_run,
    )
    if not dry_run:
        track.track_config = new_config
        track.recipe_cached = new_recipe_cached
    return "cache_refresh_failed" if cache_status == "refresh_failed" else "updated"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="Print changes without persisting."
    )
    parser.add_argument(
        "--track-id", type=str, default=None, help="Backfill a single track by UUID."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Stop after N tracks."
    )
    parser.add_argument(
        "--include-stale",
        action="store_true",
        help="Rewrite even tracks whose bounds already match rank-1.",
    )
    args = parser.parse_args()

    counts: dict[str, int] = {}
    with _sync_session() as db:
        q = db.query(MusicTrack)
        if args.track_id:
            q = q.filter(MusicTrack.id == args.track_id)
        rows: list[MusicTrack] = q.all()

        for i, track in enumerate(rows):
            if args.limit is not None and i >= args.limit:
                break
            try:
                outcome = _backfill_one(
                    track, include_stale=args.include_stale, dry_run=args.dry_run
                )
            except Exception as exc:
                log.error("backfill_unhandled", track_id=track.id, error=str(exc))
                counts["unhandled_error"] = counts.get("unhandled_error", 0) + 1
                continue
            counts[outcome] = counts.get(outcome, 0) + 1

        if not args.dry_run:
            db.commit()

    print("\nBackfill summary:")
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
