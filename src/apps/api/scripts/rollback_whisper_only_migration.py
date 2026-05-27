"""Rollback for migrate_whisper_only_rows.py.

Reads the snapshot CSV the apply phase wrote and re-applies the prior
state per-row. Refuses any row whose CURRENT state diverges from the
snapshot — the operator has presumably already started fixing rows by
hand (paste-ID flow), and overwriting them with the original whisper_only
output would clobber that work.

Usage:
    python -m scripts.rollback_whisper_only_migration \\
        --env prod --csv .local/whisper-only-rows-<timestamp>.csv \\
        [--dry-run] [--apply --confirm-changes]
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402


def _engine_url(env: str) -> str:
    if env == "prod":
        url = os.environ.get("DATABASE_URL_PROD")
        if not url:
            raise SystemExit("ERROR: --env prod requires DATABASE_URL_PROD")
        return url
    from app.config import settings  # noqa: PLC0415

    return settings.database_url.replace("+asyncpg", "").replace(
        "postgresql://", "postgresql+psycopg://"
    )


def _load_snapshot(csv_path: Path) -> list[dict]:
    with csv_path.open() as f:
        return list(csv.DictReader(f))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--env", choices=("dev", "prod"), default="dev")
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-changes", action="store_true")
    args = parser.parse_args()

    if args.apply and not args.confirm_changes:
        print("ERROR: --apply requires --confirm-changes", file=sys.stderr)
        return 1
    if not args.apply:
        args.dry_run = True

    if not args.csv.exists():
        print(f"ERROR: snapshot file not found: {args.csv}", file=sys.stderr)
        return 1

    snapshot_rows = _load_snapshot(args.csv)
    print(f"Loaded snapshot with {len(snapshot_rows)} rows from {args.csv}")

    engine = create_engine(_engine_url(args.env), future=True)
    divergent: list[str] = []
    rollback_candidates: list[dict] = []

    with engine.connect() as conn:
        for snap in snapshot_rows:
            row = (
                conn.execute(
                    text(
                        "SELECT lyrics_status, lyrics_source, lyrics_whisper_draft "
                        "FROM music_tracks WHERE id = :id"
                    ),
                    {"id": snap["track_id"]},
                )
                .mappings()
                .first()
            )
            if row is None:
                divergent.append(f"{snap['track_id']} (deleted)")
                continue
            # The expected post-migration state. Any divergence from this
            # means an admin has touched the row since the migration
            # applied — refuse to overwrite their work.
            if (
                row["lyrics_status"] != "needs_manual_lyrics"
                or row["lyrics_source"] != snap["prior_source"]
            ):
                divergent.append(
                    f"{snap['track_id']} (now status={row['lyrics_status']!r}, "
                    f"source={row['lyrics_source']!r})"
                )
                continue
            rollback_candidates.append(snap)

    print(f"Rollback candidates: {len(rollback_candidates)}")
    if divergent:
        print(f"Divergent rows (would skip): {len(divergent)}")
        for d in divergent[:20]:
            print(f"  {d}")
        if len(divergent) > 20:
            print(f"  … and {len(divergent) - 20} more")

    if divergent and not args.dry_run:
        # Strict policy: if ANY row has diverged, refuse the whole rollback.
        # The operator has work in progress that overlaps with the rows being
        # restored — partial rollback would silently destroy some of it.
        print(
            "\nERROR: refusing to apply rollback while some rows have diverged. "
            "Hand-fix the divergent rows or filter the snapshot CSV first.",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        print("\nDRY RUN — no rows mutated.")
        return 0

    typed = input(
        f"Type the rollback count ({len(rollback_candidates)}) to apply, anything else to abort: "
    ).strip()
    if typed != str(len(rollback_candidates)):
        print("Aborted — typed count didn't match.")
        return 2

    with engine.connect() as conn:
        applied = 0
        for snap in rollback_candidates:
            with conn.begin():
                conn.execute(
                    text(
                        """
                        UPDATE music_tracks
                        SET lyrics_status = :status,
                            lyrics_source = :source,
                            lyrics_cached = :cached,
                            lyrics_whisper_draft = NULL,
                            track_config = :track_config,
                            lyrics_extraction_version = :version
                        WHERE id = :id
                        """
                    ),
                    {
                        "id": snap["track_id"],
                        "status": snap["prior_status"],
                        "source": snap["prior_source"],
                        "cached": snap["prior_lyrics_cached_json"] or None,
                        "track_config": snap["prior_track_config_json"] or None,
                        "version": int(snap["prior_extraction_version"]),
                    },
                )
                applied += 1
    print(f"Rolled back {applied} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
