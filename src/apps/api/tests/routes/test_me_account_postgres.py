"""Real-Postgres serialization checks for account erasure."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.database import sync_engine


def test_account_erasure_targeted_job_delete_fails_closed_on_late_job() -> None:
    creator_id = uuid.uuid4()
    job_id = uuid.uuid4()
    with sync_engine.begin() as seed:
        seed.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": creator_id, "email": f"account-lock-{creator_id}@test.local"},
        )

    try:
        # Account erasure snapshots zero Jobs, then a direct creator commits a
        # new one. The route's target-bound DELETE cannot silently remove that
        # unseen row without externalizing its storage/revoke state.
        with sync_engine.connect() as eraser, sync_engine.connect() as creator:
            eraser_tx = eraser.begin()
            captured_job_ids = list(
                eraser.scalars(
                    text("SELECT id FROM jobs WHERE user_id = :user_id FOR UPDATE"),
                    {"user_id": creator_id},
                )
            )
            assert captured_job_ids == []

            with creator.begin():
                creator.execute(
                    text(
                        "INSERT INTO jobs (id, user_id, status, job_type, raw_storage_path) "
                        "VALUES (:id, :user_id, 'queued', 'template', :path)"
                    ),
                    {
                        "id": job_id,
                        "user_id": creator_id,
                        "path": f"users/{creator_id}/generative/late.mp4",
                    },
                )

            # No captured Job id is eligible for deletion. The final parent
            # delete sees the late FK row and aborts, preserving both records
            # so a retry can snapshot and clean them normally.
            with pytest.raises(DBAPIError, match="foreign key"):
                eraser.execute(text("DELETE FROM users WHERE id = :id"), {"id": creator_id})
            eraser_tx.rollback()

        with sync_engine.begin() as verify:
            assert (
                verify.scalar(text("SELECT count(*) FROM users WHERE id = :id"), {"id": creator_id})
                == 1
            )
            assert (
                verify.scalar(text("SELECT count(*) FROM jobs WHERE id = :id"), {"id": job_id}) == 1
            )
    finally:
        with sync_engine.begin() as cleanup:
            cleanup.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
            cleanup.execute(text("DELETE FROM users WHERE id = :id"), {"id": creator_id})
