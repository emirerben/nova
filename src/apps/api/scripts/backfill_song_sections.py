"""Backfill MusicTrack.best_sections for tracks missing sections or below current version.

Usage (from src/apps/api/):
    python -m scripts.backfill_song_sections [--dry-run] [--limit N]
                                              [--include-stale]

For each MusicTrack where ``analysis_status == 'ready'`` AND
``audio_gcs_path IS NOT NULL`` AND ``duration_s > 0`` AND
(``best_sections IS NULL`` OR ``section_version != CURRENT_SECTION_VERSION``
when ``--include-stale``), downloads the audio, uploads it to the Gemini
File API once, runs ``SongSectionsAgent`` against the same
``audio_template`` output the track was originally analyzed with (read
from ``recipe_cached``), and persists ``best_sections`` +
``section_version`` in a single transaction.

Idempotent: re-runs skip tracks already at ``CURRENT_SECTION_VERSION``
unless ``--include-stale`` is passed. Failures are logged and the loop
continues — the matcher's stale-row filter naturally excludes the
remaining unsectioned tracks.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from typing import Any

import structlog

from app.agents._model_client import default_client
from app.agents._runtime import RunContext
from app.agents._schemas.song_sections import CURRENT_SECTION_VERSION
from app.agents.song_sections import SongSectionsAgent, SongSectionsInput
from app.database import sync_session as _sync_session
from app.models import MusicTrack
from app.storage import download_to_file

log = structlog.get_logger()


def _select_targets(
    include_stale: bool, limit: int | None
) -> list[tuple[str, str, float, list[float], dict]]:
    """Return [(track_id, audio_gcs_path, duration_s, beats, recipe_cached)]
    for tracks that need backfill."""
    with _sync_session() as db:
        q = db.query(MusicTrack).filter(
            MusicTrack.analysis_status == "ready",
            MusicTrack.audio_gcs_path.isnot(None),
        )
        rows: list[MusicTrack] = q.all()

    targets: list[tuple[str, str, float, list[float], dict]] = []
    for t in rows:
        if t.best_sections is None:
            reason = "missing"
        elif include_stale and t.section_version != CURRENT_SECTION_VERSION:
            reason = "stale"
        else:
            continue
        duration_s = float(t.duration_s or 0.0)
        if duration_s <= 0.0:
            log.warning("backfill_skip_no_duration", track_id=t.id)
            continue
        targets.append(
            (
                t.id,
                t.audio_gcs_path or "",
                duration_s,
                list(t.beat_timestamps_s or []),
                dict(t.recipe_cached or {}),
            )
        )
        log.info("backfill_target", track_id=t.id, reason=reason)
        if limit is not None and len(targets) >= limit:
            break
    return targets


def _section_one(
    audio_gcs: str,
    audio_template_output: dict,
    beats: list[float],
    duration_s: float,
    track_id: str,
) -> dict | None:
    """Upload + section a single track. Returns SongSectionsOutput dict, or None."""
    # Local import keeps the script importable when the pipeline module is
    # unavailable (e.g. type-only inspection in CI).
    from app.pipeline.agents.gemini_analyzer import gemini_upload_and_wait  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="nova_song_sections_backfill_") as tmpdir:
        local_audio = os.path.join(tmpdir, "audio.m4a")
        try:
            download_to_file(audio_gcs, local_audio)
        except Exception as exc:
            log.warning("backfill_download_failed", track_id=track_id, error=str(exc))
            return None
        try:
            file_ref: Any = gemini_upload_and_wait(local_audio, timeout=120)
        except Exception as exc:
            log.warning("backfill_gemini_upload_failed", track_id=track_id, error=str(exc))
            return None
        try:
            inp = SongSectionsInput(
                file_uri=file_ref.uri,
                file_mime=getattr(file_ref, "mime_type", None) or "audio/mp4",
                duration_s=duration_s,
                beat_timestamps_s=beats,
                audio_template_output=audio_template_output or {},
            )
            out = SongSectionsAgent(default_client()).run(
                inp, ctx=RunContext(job_id=f"backfill:{track_id}")
            )
            return out.to_dict()
        except Exception as exc:
            log.warning("backfill_section_failed", track_id=track_id, error=str(exc))
            return None


def _persist(track_id: str, sections_dict: dict) -> None:
    with _sync_session() as db:
        track = db.get(MusicTrack, track_id)
        if track is None:
            log.warning("backfill_track_missing_at_persist", track_id=track_id)
            return
        track.best_sections = sections_dict.get("sections")
        track.section_version = sections_dict.get("section_version") or None
        db.commit()
    log.info("backfill_persisted", track_id=track_id, section_version=CURRENT_SECTION_VERSION)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List targets and exit; no LLM calls, no DB writes.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max tracks to process this run.",
    )
    parser.add_argument(
        "--include-stale",
        action="store_true",
        help=(
            f"Re-section tracks where section_version != {CURRENT_SECTION_VERSION}. "
            "Default re-sections only tracks where best_sections IS NULL."
        ),
    )
    args = parser.parse_args()

    targets = _select_targets(args.include_stale, args.limit)
    log.info(
        "backfill_start",
        count=len(targets),
        dry_run=args.dry_run,
        include_stale=args.include_stale,
        section_version=CURRENT_SECTION_VERSION,
    )

    if args.dry_run:
        for track_id, _, _, _, _ in targets:
            print(track_id)
        return 0

    succeeded = 0
    failed = 0
    for track_id, audio_gcs, duration_s, beats, recipe_cached in targets:
        sections = _section_one(audio_gcs, recipe_cached, beats, duration_s, track_id)
        if sections is None:
            failed += 1
            continue
        _persist(track_id, sections)
        succeeded += 1

    log.info("backfill_done", succeeded=succeeded, failed=failed, total=len(targets))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
