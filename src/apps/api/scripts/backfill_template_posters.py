"""Backfill thumbnail_gcs_path for templates that were analyzed before
poster extraction was wired into the pipeline.

Usage (from src/apps/api/):
    python -m scripts.backfill_template_posters [--dry-run] [--limit N]

For each VideoTemplate where analysis_status == 'ready' AND
thumbnail_gcs_path IS NULL, downloads the template video, runs FFmpeg to
extract a poster JPEG, uploads to GCS, and persists the path. Idempotent:
re-runs skip already-backfilled rows.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

import structlog

from app.database import sync_session as _sync_session
from app.models import VideoTemplate
from app.services.template_poster import (
    PosterExtractionError,
    generate_and_upload as generate_poster,
)
from app.storage import download_to_file

log = structlog.get_logger()


def backfill_one(template_id: str, gcs_path: str) -> str | None:
    """Download + extract + upload for a single template. Returns the new
    thumbnail_gcs_path on success, or None on failure (logged)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = os.path.join(tmpdir, "template.mp4")
        try:
            download_to_file(gcs_path, local_path)
        except Exception as exc:
            log.error("backfill_download_failed", template_id=template_id, error=str(exc))
            return None
        try:
            return generate_poster(template_id, local_path)
        except PosterExtractionError as exc:
            log.error("backfill_extract_failed", template_id=template_id, error=str(exc))
            return None
        except Exception as exc:
            log.error("backfill_upload_failed", template_id=template_id, error=str(exc))
            return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="List candidates only, do not modify GCS or DB.")
    parser.add_argument("--limit", type=int, default=0, help="Cap the number of templates processed (0 = no cap).")
    args = parser.parse_args()

    with _sync_session() as db:
        candidates = db.query(VideoTemplate).filter(
            VideoTemplate.analysis_status == "ready",
            VideoTemplate.thumbnail_gcs_path.is_(None),
            VideoTemplate.gcs_path.isnot(None),
        ).all()

    if args.limit > 0:
        candidates = candidates[: args.limit]

    print(f"Found {len(candidates)} templates needing posters.", flush=True)
    if args.dry_run:
        for t in candidates:
            print(f"  [DRY RUN] {t.id}  {t.gcs_path}")
        return 0

    succeeded = 0
    failed = 0
    for i, t in enumerate(candidates, 1):
        print(f"[{i}/{len(candidates)}] {t.id} ...", flush=True)
        new_path = backfill_one(t.id, t.gcs_path)
        if new_path is None:
            failed += 1
            continue
        # Conditional update — only writes if thumbnail_gcs_path is still NULL.
        # Two parallel backfill runs won't clobber each other's poster.
        with _sync_session() as db:
            updated = db.query(VideoTemplate).filter(
                VideoTemplate.id == t.id,
                VideoTemplate.thumbnail_gcs_path.is_(None),
            ).update(
                {"thumbnail_gcs_path": new_path},
                synchronize_session=False,
            )
            db.commit()
        if updated:
            succeeded += 1
            print(f"  ✓ {new_path}", flush=True)
        else:
            print(f"  ⚠ skipped (already set by another run): {t.id}", flush=True)

    print(f"\nBackfill complete: {succeeded} succeeded, {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
