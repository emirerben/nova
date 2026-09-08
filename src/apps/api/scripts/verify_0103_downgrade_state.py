#!/usr/bin/env python3
"""Seed and verify legal 0103-only state around CI's serial downgrade."""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import text

from app.database import sync_engine

CREATOR_A = uuid.UUID("01030103-0000-4000-8000-000000000001")
CREATOR_B = uuid.UUID("01030103-0000-4000-8000-000000000002")
MANIFEST_ID = uuid.UUID("01030103-0000-4000-8000-000000000003")
ENTRY_ID = uuid.UUID("01030103-0000-4000-8000-000000000004")
SOURCE_IDENTITY = "0103-downgrade-duplicate"


def _revision(connection) -> str:  # noqa: ANN001
    return str(connection.scalar(text("SELECT version_num FROM alembic_version")))


def seed() -> None:
    with sync_engine.begin() as connection:
        if _revision(connection) != "0103":
            raise RuntimeError("verify_0103_downgrade_state seed requires revision 0103")
        connection.execute(
            text("INSERT INTO users (id, email) VALUES (:a, :email_a), (:b, :email_b)"),
            {
                "a": CREATOR_A,
                "b": CREATOR_B,
                "email_a": "0103-downgrade-a@test.local",
                "email_b": "0103-downgrade-b@test.local",
            },
        )
        connection.execute(
            text(
                "INSERT INTO storage_retention_manifests "
                "(id, status, generated_at, report_only_until, summary_json) "
                "VALUES (:id, 'approved', clock_timestamp(), clock_timestamp(), '{}'::jsonb)"
            ),
            {"id": MANIFEST_ID},
        )
        connection.execute(
            text(
                "INSERT INTO storage_retention_entries "
                "(id, manifest_id, creator_id, object_path, object_generation, action, "
                "reason, eligible_at, status, deleted_at) VALUES "
                "(:id, :manifest_id, :creator_id, :path, '1', 'delete', 'ci_downgrade', "
                "clock_timestamp() - interval '1 day', 'deleting', clock_timestamp())"
            ),
            {
                "id": ENTRY_ID,
                "manifest_id": MANIFEST_ID,
                "creator_id": CREATOR_A,
                "path": f"users/{CREATOR_A}/0103-downgrade.mp4",
            },
        )
        for index, creator_id in enumerate((CREATOR_A, CREATOR_B), start=1):
            connection.execute(
                text(
                    "INSERT INTO media_analysis_cache "
                    "(id, creator_id, source_identity, analyzer, model, prompt_version, "
                    "schema_version, result_json, expires_at) VALUES "
                    "(:id, :creator_id, :identity, 'clip', 'test-model', 'v1', 'v1', "
                    "'{}'::jsonb, clock_timestamp() + interval '1 hour')"
                ),
                {
                    "id": uuid.UUID(f"01030103-0000-4000-8000-00000000000{4 + index}"),
                    "creator_id": creator_id,
                    "identity": SOURCE_IDENTITY,
                },
            )


def verify_and_clean() -> None:
    with sync_engine.begin() as connection:
        if _revision(connection) != "0102":
            raise RuntimeError("verify_0103_downgrade_state verify requires revision 0102")
        status = connection.scalar(
            text("SELECT status FROM storage_retention_entries WHERE id = :id"),
            {"id": ENTRY_ID},
        )
        if status != "failed":
            raise RuntimeError(f"0103 downgrade retained unsafe status {status!r}")
        cache_rows = connection.scalar(
            text("SELECT count(*) FROM media_analysis_cache WHERE source_identity = :identity"),
            {"identity": SOURCE_IDENTITY},
        )
        if cache_rows != 0:
            raise RuntimeError(f"0103 downgrade retained {cache_rows} duplicate cache rows")
        connection.execute(
            text("DELETE FROM storage_retention_entries WHERE id = :id"), {"id": ENTRY_ID}
        )
        connection.execute(
            text("DELETE FROM storage_retention_manifests WHERE id = :id"),
            {"id": MANIFEST_ID},
        )
        connection.execute(
            text("DELETE FROM users WHERE id IN (:a, :b)"),
            {"a": CREATOR_A, "b": CREATOR_B},
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("seed", "verify"))
    args = parser.parse_args()
    seed() if args.mode == "seed" else verify_and_clean()


if __name__ == "__main__":
    main()
