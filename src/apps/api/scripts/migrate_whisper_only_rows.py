"""Bulk-migrate stale `whisper_only` lyrics rows to needs_manual_lyrics.

Beauty And A Beat PR (2026-05-27). Schema migration 0033 added three new
columns (`lyrics_diagnostic`, `lyrics_whisper_draft`, `lyrics_extraction_version`).
This script is the SEPARATE destructive data migration:

  For every MusicTrack row with `lyrics_source = 'whisper_only'`
  AND `lyrics_status = 'ready'`:
    1. Move the existing `lyrics_cached` blob into `lyrics_whisper_draft`
       (preserve the Whisper output for admin reference).
    2. Set `lyrics_cached = NULL` (production consumers must never see
       whisper_only lyrics now).
    3. Set `lyrics_status = 'needs_manual_lyrics'` so the admin UI's
       recovery flow kicks in.
    4. Force `track_config.lyrics_config.enabled = false` so any music
       job using this track degrades to no-overlay rather than reaching
       the injector with stale `lyrics_cached`.
    5. Bump `lyrics_extraction_version` (defensive — keeps the stale-task
       gate honest if any in-flight extraction was running pre-migration).

The script writes a snapshot CSV BEFORE mutating anything, so a sibling
rollback script (`rollback_whisper_only_migration.py`) can restore the
prior state if the change breaks something.

Usage (from src/apps/api/):
    # Phase A — dry run. Default. Reads ONLY; writes the snapshot CSV
    # for visibility.
    python -m scripts.migrate_whisper_only_rows --env prod --dry-run

    # Phase B — apply. Refuses to run unless --confirm-changes is
    # supplied AND the operator types the affected count when prompted.
    python -m scripts.migrate_whisper_only_rows --env prod --apply --confirm-changes

Per-row transaction: a mid-run DB connection loss leaves a partial-but-
consistent state. The script is idempotent — re-runs skip rows already
at `needs_manual_lyrics`.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Path bootstrap so `python -m scripts.foo` works the same way the existing
# scripts (backfill_song_classifier etc.) do, without an editable install.
_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

# Import after path bootstrap so app.config picks up the right .env.
from sqlalchemy import create_engine, text  # noqa: E402

SNAPSHOT_DIR = Path(".local")


def _engine_url(env: str) -> str:
    """Resolve a DATABASE_URL by env name.

    - "dev" / "local": read from the local .env via the app config loader.
    - "prod": require DATABASE_URL_PROD explicitly so a slip of the
      `--env prod` flag against an unset terminal doesn't accidentally
      target dev.
    """
    if env == "prod":
        url = os.environ.get("DATABASE_URL_PROD")
        if not url:
            raise SystemExit(
                "ERROR: --env prod requires DATABASE_URL_PROD in the environment. "
                "Source the prod env file or paste the URL inline."
            )
        return url
    # Local / dev — fall through to app.config which reads .env.
    from app.config import settings  # noqa: PLC0415

    url = settings.database_url
    # SQLAlchemy sync engine needs psycopg2/psycopg dialect, not asyncpg.
    return url.replace("+asyncpg", "").replace("postgresql://", "postgresql+psycopg://")


def _select_affected_rows(conn) -> list[dict]:
    """Read all `whisper_only` + `ready` rows. Each row carries everything
    the snapshot CSV needs AND everything the per-row UPDATE needs."""
    sql = text(
        """
        SELECT
            id,
            title,
            artist,
            lyrics_status,
            lyrics_source,
            lyrics_cached,
            track_config,
            lyrics_extraction_version
        FROM music_tracks
        WHERE lyrics_source = 'whisper_only'
          AND lyrics_status = 'ready'
        ORDER BY id
        """
    )
    rows = []
    for row in conn.execute(sql).mappings():
        rows.append(dict(row))
    return rows


def _write_snapshot(rows: list[dict], snapshot_path: Path) -> None:
    """One CSV row per affected track. Columns are chosen so the rollback
    script can restore the prior state by name without needing schema
    introspection."""
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    with snapshot_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "track_id",
                "prior_status",
                "prior_source",
                "prior_lyrics_config_enabled",
                "prior_lyrics_cached_lines_count",
                "prior_extraction_version",
                "mutated_at",
                "prior_lyrics_cached_json",
                "prior_track_config_json",
            ]
        )
        for r in rows:
            cached = r["lyrics_cached"] or {}
            tc = r["track_config"] or {}
            lyrics_cfg = (tc.get("lyrics_config") or {}) if isinstance(tc, dict) else {}
            writer.writerow(
                [
                    r["id"],
                    r["lyrics_status"],
                    r["lyrics_source"],
                    bool(lyrics_cfg.get("enabled")),
                    len((cached.get("lines") or []) if isinstance(cached, dict) else []),
                    int(r["lyrics_extraction_version"] or 0),
                    datetime.now(UTC).isoformat(),
                    json.dumps(cached),
                    json.dumps(tc),
                ]
            )


def _apply_migration(conn, rows: list[dict]) -> tuple[int, int]:
    """Mutate each row in its own transaction.

    Returns (mutated, skipped). Skipped rows already had a non-`whisper_only`
    status at apply time (idempotent re-run, or a concurrent admin edit).
    """
    mutated = 0
    skipped = 0
    for r in rows:
        track_id = r["id"]

        # Re-read inside the per-row transaction with FOR UPDATE so we
        # don't race a concurrent extraction task that might be finishing.
        with conn.begin():
            current = (
                conn.execute(
                    text(
                        "SELECT lyrics_source, lyrics_status, lyrics_cached, "
                        "track_config, lyrics_extraction_version "
                        "FROM music_tracks WHERE id = :id FOR UPDATE"
                    ),
                    {"id": track_id},
                )
                .mappings()
                .first()
            )
            if current is None:
                skipped += 1
                continue
            if not (
                current["lyrics_source"] == "whisper_only" and current["lyrics_status"] == "ready"
            ):
                # Pre-empted by an admin action or a prior partial run.
                skipped += 1
                continue

            cached = current["lyrics_cached"]
            tc = dict(current["track_config"] or {})
            existing_lyrics_cfg = dict(tc.get("lyrics_config") or {})
            existing_lyrics_cfg["enabled"] = False
            tc["lyrics_config"] = existing_lyrics_cfg
            new_version = int(current["lyrics_extraction_version"] or 0) + 1

            conn.execute(
                text(
                    """
                    UPDATE music_tracks
                    SET lyrics_status = 'needs_manual_lyrics',
                        lyrics_whisper_draft = :draft,
                        lyrics_cached = NULL,
                        lyrics_error_detail = :err,
                        track_config = :track_config,
                        lyrics_extraction_version = :new_version
                    WHERE id = :id
                    """
                ),
                {
                    "id": track_id,
                    "draft": json.dumps(cached) if cached is not None else None,
                    "err": ("Migrated from whisper_only — paste an LRCLIB ID to recover."),
                    "track_config": json.dumps(tc),
                    "new_version": new_version,
                },
            )
            mutated += 1
    return mutated, skipped


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--env", choices=("dev", "prod"), default="dev")
    parser.add_argument(
        "--dry-run", action="store_true", help="Default. Print + snapshot; do not mutate."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually mutate. Requires --confirm-changes AND a count-typed confirmation.",
    )
    parser.add_argument(
        "--confirm-changes",
        action="store_true",
        help="Second-factor confirmation flag for --apply.",
    )
    args = parser.parse_args()

    if args.apply and not args.confirm_changes:
        print(
            "ERROR: --apply requires --confirm-changes (second factor). Refusing to mutate.",
            file=sys.stderr,
        )
        return 1
    # Default to dry-run when neither flag is set.
    if not args.apply:
        args.dry_run = True

    engine = create_engine(_engine_url(args.env), future=True)

    with engine.connect() as conn:
        rows = _select_affected_rows(conn)

    affected_count = len(rows)
    print(
        "\nAffected rows (lyrics_source='whisper_only' AND lyrics_status='ready'): "
        f"{affected_count}"
    )
    if affected_count == 0:
        print("Nothing to do.")
        return 0

    # Source distribution sanity — not strictly informative since the WHERE
    # already pins source, but useful as a paranoia check for future schema
    # changes that add new sources.
    print("Source distribution:")
    seen: dict[str, int] = {}
    for r in rows:
        seen[r["lyrics_source"]] = seen.get(r["lyrics_source"], 0) + 1
    for src, n in sorted(seen.items()):
        print(f"  {src}: {n}")

    print("\nFirst 50 affected track IDs:")
    for r in rows[:50]:
        print(f"  {r['id']}  —  {r['title']}  —  {r['artist']}")
    if affected_count > 50:
        print(f"  … and {affected_count - 50} more")

    # Snapshot CSV — written in both phases so the audit trail exists
    # regardless of whether the operator follows through with --apply.
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    snapshot_path = SNAPSHOT_DIR / f"whisper-only-rows-{timestamp}.csv"
    _write_snapshot(rows, snapshot_path)
    print(f"\nSnapshot CSV written to: {snapshot_path}")

    if args.dry_run:
        print("\nDRY RUN — no rows mutated. Pass --apply --confirm-changes to commit.")
        return 0

    # Interactive count-confirmation: the operator must type the count
    # back. This is the "are you sure?" gate against a typo-flag invocation.
    typed = input(
        f"\nType the affected count ({affected_count}) to apply, or anything else to abort: "
    ).strip()
    if typed != str(affected_count):
        print("Aborted — typed count didn't match.")
        return 2

    with engine.connect() as conn:
        mutated, skipped = _apply_migration(conn, rows)

    print(f"\nMigration complete. Mutated: {mutated}. Skipped (state diverged): {skipped}.")
    print(f"Snapshot retained at: {snapshot_path}")
    print(
        "Rollback (if needed): python -m scripts.rollback_whisper_only_migration "
        f"--env {args.env} --csv {snapshot_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
