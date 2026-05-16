"""Bulk-ingest a YouTube playlist into Nova's music library.

Usage (from src/apps/api/):
    python -m scripts.ingest_youtube_playlist <playlist_url> [--dry-run]
                                                [--limit N] [--skip-analysis]

Flow per playlist entry:
  1. Extract the video id via ``yt_dlp.YoutubeDL(extract_flat=True)``.
  2. Normalize to a canonical URL (``https://www.youtube.com/watch?v=<id>``)
     so re-runs against the same playlist are no-ops (idempotent).
  3. If a ``MusicTrack`` with that ``source_url`` exists, skip.
  4. Otherwise download audio + upload to GCS via the existing
     ``download_audio_and_upload`` route helper, insert the
     ``MusicTrack`` row, and dispatch ``analyze_music_track_task`` so the
     full Phase 0-2 analysis pipeline runs (beats, audio_template,
     song_classifier, song_sections).

Flags:
  --dry-run         List would-be inserts and exit. No downloads, no DB
                    writes.
  --limit N         Cap the number of new ingests this run.
  --skip-analysis   Insert rows but do NOT dispatch the Celery analysis
                    task. Useful for admin-side debugging where the task
                    will be triggered manually via ``/admin/music-tracks/
                    <id>/reanalyze``.

Errors per entry are non-fatal: log + continue. Final log shows
``ingested=N skipped=M failed=K``. Exit 0 unless ``failed > 0``.
"""

from __future__ import annotations

import argparse
import sys
import uuid

import structlog
import yt_dlp

from app.database import sync_session as _sync_session
from app.models import MusicTrack
from app.services.audio_download import DownloadError, download_audio_and_upload

log = structlog.get_logger()

_CANONICAL_URL_TEMPLATE = "https://www.youtube.com/watch?v={video_id}"


class PlaylistEntry:
    """One entry from a flat-extracted playlist."""

    __slots__ = ("video_id", "title", "uploader", "duration_s")

    def __init__(
        self,
        video_id: str,
        title: str,
        uploader: str | None,
        duration_s: float | None,
    ) -> None:
        self.video_id = video_id
        self.title = title
        self.uploader = uploader or ""
        self.duration_s = duration_s

    @property
    def canonical_url(self) -> str:
        return _CANONICAL_URL_TEMPLATE.format(video_id=self.video_id)


def _list_playlist(playlist_url: str) -> list[PlaylistEntry]:
    """Return a flat list of playlist entries.

    Uses ``extract_flat=True`` so yt-dlp only resolves the playlist
    metadata, not each video — fast and cheap (one HTTP request).
    Skips entries with no ``id`` (private/deleted videos).
    """
    opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(playlist_url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise SystemExit(f"Failed to list playlist: {exc}") from exc

    if not isinstance(info, dict):
        raise SystemExit(f"Unexpected yt-dlp result type: {type(info).__name__}")

    raw_entries = info.get("entries") or []
    entries: list[PlaylistEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        video_id = raw.get("id")
        if not isinstance(video_id, str) or not video_id:
            log.warning("playlist_entry_no_id", title=raw.get("title"))
            continue
        title = str(raw.get("title") or f"Track {video_id}")
        uploader = raw.get("uploader") or raw.get("channel")
        duration = raw.get("duration")
        duration_s = float(duration) if isinstance(duration, (int, float)) else None
        entries.append(PlaylistEntry(video_id, title, uploader, duration_s))
    return entries


def _existing_source_urls(canonical_urls: list[str]) -> set[str]:
    """Return the subset of canonical_urls that already have a MusicTrack row."""
    if not canonical_urls:
        return set()
    with _sync_session() as db:
        rows = (
            db.query(MusicTrack.source_url).filter(MusicTrack.source_url.in_(canonical_urls)).all()
        )
    return {row[0] for row in rows}


def _ingest_one(entry: PlaylistEntry, skip_analysis: bool) -> str | None:
    """Download + insert one entry. Returns the new track_id or None on failure."""
    canonical_url = entry.canonical_url
    track_id = str(uuid.uuid4())

    try:
        gcs_path, duration_s, thumbnail_url = download_audio_and_upload(canonical_url)
    except DownloadError as exc:
        log.warning(
            "ingest_download_failed",
            video_id=entry.video_id,
            title=entry.title,
            error=str(exc),
        )
        return None

    track = MusicTrack(
        id=track_id,
        title=entry.title.strip() or f"Track {entry.video_id}",
        artist=entry.uploader,
        source_url=canonical_url,
        audio_gcs_path=gcs_path,
        duration_s=duration_s,
        thumbnail_url=thumbnail_url,
        analysis_status="queued",
    )

    try:
        with _sync_session() as db:
            db.add(track)
            db.commit()
    except Exception as exc:
        log.warning(
            "ingest_db_insert_failed",
            video_id=entry.video_id,
            error=str(exc),
        )
        return None

    if not skip_analysis:
        # Local import: mirrors the route's deferred import pattern in
        # admin_music.py so this script can be invoked even when Celery
        # isn't available locally (e.g. import-time smoke checks).
        from app.tasks.music_orchestrate import analyze_music_track_task  # noqa: PLC0415

        analyze_music_track_task.delay(track_id)

    log.info(
        "ingest_done",
        track_id=track_id,
        video_id=entry.video_id,
        title=entry.title,
        dispatched=not skip_analysis,
    )
    return track_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "playlist_url",
        help="YouTube playlist URL (e.g. https://www.youtube.com/playlist?list=PLxxx)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List would-be inserts and exit. No downloads, no DB writes.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of new tracks to ingest this run.",
    )
    parser.add_argument(
        "--skip-analysis",
        action="store_true",
        help=(
            "Insert MusicTrack rows but do NOT dispatch analyze_music_track_task. "
            "Useful for admin-side debugging."
        ),
    )
    args = parser.parse_args()

    entries = _list_playlist(args.playlist_url)
    log.info("playlist_listed", count=len(entries), playlist_url=args.playlist_url)

    canonical_urls = [e.canonical_url for e in entries]
    already_present = _existing_source_urls(canonical_urls)

    new_entries = [e for e in entries if e.canonical_url not in already_present]
    if args.limit is not None:
        new_entries = new_entries[: args.limit]

    log.info(
        "ingest_plan",
        total=len(entries),
        already_present=len(already_present),
        new=len(new_entries),
        skip_analysis=args.skip_analysis,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        for entry in new_entries:
            print(f"{entry.canonical_url}\t{entry.title}")
        return 0

    ingested = 0
    failed = 0
    for entry in new_entries:
        track_id = _ingest_one(entry, args.skip_analysis)
        if track_id is None:
            failed += 1
        else:
            ingested += 1

    skipped = len(already_present)
    log.info(
        "playlist_ingest_done",
        ingested=ingested,
        skipped=skipped,
        failed=failed,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
