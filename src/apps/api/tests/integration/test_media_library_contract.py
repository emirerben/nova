# ruff: noqa: E501
"""Real-Postgres ownership and immutability contract for KRI-384's library base."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.models import Base

_url = os.environ.get("DATABASE_URL", "")
if (
    not (make_url(_url).database or "").endswith("_test")
    and make_url(_url).database != "nova_kri384_contract"
):
    pytest.skip(
        "media-library contract tests require a disposable test database", allow_module_level=True
    )

_engine = create_engine(_url.replace("postgresql+asyncpg://", "postgresql://"), pool_pre_ping=True)


@pytest.fixture(scope="module", autouse=True)
def _schema_ready() -> None:
    # This is deliberately an integration contract: Alembic must have created
    # the same tables and triggers that the ORM metadata describes.
    with _engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT to_regclass('public.media_assets')")) == "media_assets"
        )
        assert "media_assets" in Base.metadata.tables
    yield
    _engine.dispose()


def _insert_user(connection: object) -> uuid.UUID:
    user_id = uuid.uuid4()
    connection.execute(
        text("INSERT INTO users (id, email) VALUES (:id, :email)"),
        {"id": user_id, "email": f"{user_id}@library.test"},
    )
    return user_id


def _insert_asset(connection: object, user_id: uuid.UUID) -> uuid.UUID:
    asset_id = uuid.uuid4()
    connection.execute(
        text("INSERT INTO media_assets (id, user_id, kind) VALUES (:id, :user_id, 'video')"),
        {"id": asset_id, "user_id": user_id},
    )
    return asset_id


def _insert_source(connection: object, user_id: uuid.UUID, asset_id: uuid.UUID) -> uuid.UUID:
    source_id = uuid.uuid4()
    connection.execute(
        text("""
            INSERT INTO media_asset_sources
              (id, user_id, asset_id, role, object_path, object_generation, byte_count, content_type)
            VALUES (:id, :user_id, :asset_id, 'original', :path, 'gen-1', 42, 'video/mp4')
        """),
        {
            "id": source_id,
            "user_id": user_id,
            "asset_id": asset_id,
            "path": f"users/{user_id}/library/a.mp4",
        },
    )
    return source_id


def _assert_rejected(connection: object, statement: object, params: dict[str, object]) -> None:
    with pytest.raises(DBAPIError):
        with connection.begin_nested():
            connection.execute(statement, params)


def test_cross_tenant_asset_source_and_collection_writes_are_rejected() -> None:
    with _engine.begin() as connection:
        first = _insert_user(connection)
        second = _insert_user(connection)
        asset = _insert_asset(connection, first)
        _assert_rejected(
            connection,
            text("""
                    INSERT INTO media_asset_sources
                      (id, user_id, asset_id, role, object_path, object_generation, byte_count, content_type)
                    VALUES (:id, :user_id, :asset_id, 'original', :path, 'g', 1, 'video/mp4')
                """),
            {
                "id": uuid.uuid4(),
                "user_id": second,
                "asset_id": asset,
                "path": f"users/{second}/bad.mp4",
            },
        )
        collection = uuid.uuid4()
        connection.execute(
            text(
                "INSERT INTO media_collections (id, user_id, name) VALUES (:id, :user_id, 'Mine')"
            ),
            {"id": collection, "user_id": second},
        )
        _assert_rejected(
            connection,
            text(
                "INSERT INTO media_collection_members (collection_id, asset_id, user_id) VALUES (:collection, :asset, :user_id)"
            ),
            {"collection": collection, "asset": asset, "user_id": second},
        )
        job_id = uuid.uuid4()
        connection.execute(
            text(
                "INSERT INTO jobs (id, user_id, raw_storage_path, status, job_type) "
                "VALUES (:id, :user_id, 'users/job.mp4', 'queued', 'default')"
            ),
            {"id": job_id, "user_id": second},
        )
        _assert_rejected(
            connection,
            text("""
                INSERT INTO media_asset_references (id, user_id, asset_id, job_id)
                VALUES (:id, :user_id, :asset_id, :job_id)
            """),
            {"id": uuid.uuid4(), "user_id": first, "asset_id": asset, "job_id": job_id},
        )


def test_source_identity_and_asset_declared_identity_are_immutable() -> None:
    with _engine.begin() as connection:
        user_id = _insert_user(connection)
        asset_id = _insert_asset(connection, user_id)
        source_id = _insert_source(connection, user_id, asset_id)
        connection.execute(
            text(
                "UPDATE media_assets SET original_declared_sha256 = 'declared', original_byte_count = 42 WHERE id = :id"
            ),
            {"id": asset_id},
        )
        _assert_rejected(
            connection,
            text("UPDATE media_assets SET original_declared_sha256 = 'other' WHERE id = :id"),
            {"id": asset_id},
        )
        _assert_rejected(
            connection,
            text("UPDATE media_asset_sources SET object_generation = 'gen-2' WHERE id = :id"),
            {"id": source_id},
        )
        _assert_rejected(
            connection,
            text("""
                INSERT INTO media_asset_sources
                  (user_id, asset_id, role, object_path, object_generation, byte_count, content_type)
                VALUES (:owner, :asset, 'original', :path, '42', 1, 'video/mp4')
            """),
            {"owner": user_id, "asset": asset_id, "path": f"users/{user_id}/foo/.."},
        )
        connection.execute(
            text("UPDATE media_asset_sources SET uploaded_sha256 = :digest WHERE id = :id"),
            {"digest": "a" * 64, "id": source_id},
        )
        _assert_rejected(
            connection,
            text("UPDATE media_asset_sources SET uploaded_sha256 = :digest WHERE id = :id"),
            {"digest": "b" * 64, "id": source_id},
        )


@pytest.mark.parametrize("delete_consumer_first", [False, True])
def test_consumer_delete_keeps_shared_source_and_analysis_and_account_delete_purges(
    delete_consumer_first: bool,
) -> None:
    with _engine.begin() as connection:
        user_id = _insert_user(connection)
        asset_id = _insert_asset(connection, user_id)
        source_id = _insert_source(connection, user_id, asset_id)
        job_id = uuid.uuid4()
        connection.execute(
            text(
                "INSERT INTO jobs (id, user_id, raw_storage_path, status, job_type) VALUES (:id, :user_id, 'users/job.mp4', 'queued', 'default')"
            ),
            {"id": job_id, "user_id": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO media_asset_references (id, user_id, asset_id, source_id, job_id) VALUES (:id, :user_id, :asset_id, :source_id, :job_id)"
            ),
            {
                "id": uuid.uuid4(),
                "user_id": user_id,
                "asset_id": asset_id,
                "source_id": source_id,
                "job_id": job_id,
            },
        )
        connection.execute(
            text("""INSERT INTO media_asset_analyses (id, user_id, asset_id, source_id, analyzer, model, prompt_version, schema_version, source_identity)
            VALUES (:id, :user_id, :asset_id, :source_id, 'clip', 'test', '1', '1', 'source:gen-1')"""),
            {"id": uuid.uuid4(), "user_id": user_id, "asset_id": asset_id, "source_id": source_id},
        )
        if delete_consumer_first:
            connection.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
        assert connection.scalar(
            text("SELECT count(*) FROM media_asset_references WHERE asset_id = :id"),
            {"id": asset_id},
        ) == (0 if delete_consumer_first else 1)
        assert (
            connection.scalar(
                text("SELECT count(*) FROM media_asset_sources WHERE id = :id"), {"id": source_id}
            )
            == 1
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM media_asset_analyses WHERE asset_id = :id"),
                {"id": asset_id},
            )
            == 1
        )
        if not delete_consumer_first:
            # Existing Jobs are explicitly removed by account erasure before
            # the user (their legacy FK is RESTRICT). Threads cascade from
            # the user, so keep a live source-pinned thread reference here.
            connection.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
            thread_id = uuid.uuid4()
            connection.execute(
                text("INSERT INTO creation_threads (id, creator_id) VALUES (:id, :owner)"),
                {"id": thread_id, "owner": user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO media_asset_references (id, user_id, asset_id, source_id, creation_thread_id) VALUES (:id, :owner, :asset, :source, :thread)"
                ),
                {
                    "id": uuid.uuid4(),
                    "owner": user_id,
                    "asset": asset_id,
                    "source": source_id,
                    "thread": thread_id,
                },
            )
        connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        assert (
            connection.scalar(
                text("SELECT count(*) FROM media_assets WHERE id = :id"), {"id": asset_id}
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM media_asset_sources WHERE id = :id"), {"id": source_id}
            )
            == 0
        )


@pytest.mark.parametrize("asset_wide", [False, True])
def test_reference_use_and_removal_reset_source_inactivity(asset_wide: bool) -> None:
    with _engine.begin() as connection:
        user_id = _insert_user(connection)
        asset_id = _insert_asset(connection, user_id)
        source_id = _insert_source(connection, user_id, asset_id)
        old_activity = datetime.now(UTC) - timedelta(days=100)
        connection.execute(
            text("UPDATE media_asset_sources SET last_used_at = :old WHERE id = :id"),
            {"old": old_activity, "id": source_id},
        )
        job_id = uuid.uuid4()
        connection.execute(
            text(
                "INSERT INTO jobs (id, user_id, raw_storage_path, status, job_type) VALUES (:id, :owner, 'users/job.mp4', 'queued', 'default')"
            ),
            {"id": job_id, "owner": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO media_asset_references (id, user_id, asset_id, source_id, job_id) VALUES (:id, :owner, :asset, :source, :job)"
            ),
            {
                "id": uuid.uuid4(),
                "owner": user_id,
                "asset": asset_id,
                "source": None if asset_wide else source_id,
                "job": job_id,
            },
        )
        assert (
            connection.scalar(
                text("SELECT last_used_at FROM media_asset_sources WHERE id = :id"),
                {"id": source_id},
            )
            > old_activity
        )
        connection.execute(
            text("UPDATE media_asset_sources SET last_used_at = :old WHERE id = :id"),
            {"old": old_activity, "id": source_id},
        )
        connection.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
        assert (
            connection.scalar(
                text("SELECT last_used_at FROM media_asset_sources WHERE id = :id"),
                {"id": source_id},
            )
            > old_activity
        )
        connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


def test_asset_wide_reference_ignores_expired_history_but_explicit_source_is_rejected() -> None:
    with _engine.begin() as connection:
        user_id = _insert_user(connection)
        asset_id = _insert_asset(connection, user_id)
        source_id = _insert_source(connection, user_id, asset_id)
        connection.execute(
            text("UPDATE media_asset_sources SET status = 'deleted' WHERE id = :id"),
            {"id": source_id},
        )
        job_id = uuid.uuid4()
        connection.execute(
            text(
                "INSERT INTO jobs (id, user_id, raw_storage_path, status, job_type) VALUES (:id, :owner, 'users/job.mp4', 'queued', 'default')"
            ),
            {"id": job_id, "owner": user_id},
        )
        _assert_rejected(
            connection,
            text(
                "INSERT INTO media_asset_references (id, user_id, asset_id, source_id, job_id) VALUES (:id, :owner, :asset, :source, :job)"
            ),
            {
                "id": uuid.uuid4(),
                "owner": user_id,
                "asset": asset_id,
                "source": source_id,
                "job": job_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO media_asset_references (id, user_id, asset_id, job_id) VALUES (:id, :owner, :asset, :job)"
            ),
            {"id": uuid.uuid4(), "owner": user_id, "asset": asset_id, "job": job_id},
        )
        assert (
            connection.scalar(
                text("SELECT status FROM media_asset_sources WHERE id = :id"), {"id": source_id}
            )
            == "deleted"
        )
        connection.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
        connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


def test_reference_writer_rejects_retention_tombstone() -> None:
    with _engine.begin() as connection:
        user_id = _insert_user(connection)
        asset_id = _insert_asset(connection, user_id)
        source_id = _insert_source(connection, user_id, asset_id)
        manifest_id = uuid.uuid4()
        path = f"users/{user_id}/library/a.mp4"
        connection.execute(
            text("""INSERT INTO storage_retention_manifests (id, generated_at, report_only_until)
            VALUES (:id, :now, :now)"""),
            {"id": manifest_id, "now": datetime.now(UTC)},
        )
        connection.execute(
            text("""INSERT INTO storage_retention_entries (id, manifest_id, creator_id, object_path, object_generation, action, reason, eligible_at, status)
            VALUES (:id, :manifest, :user_id, :path, 'gen-1', 'delete', 'test', :now, 'deleting')"""),
            {
                "id": uuid.uuid4(),
                "manifest": manifest_id,
                "user_id": user_id,
                "path": path,
                "now": datetime.now(UTC),
            },
        )
        job_id = uuid.uuid4()
        connection.execute(
            text(
                "INSERT INTO jobs (id, user_id, raw_storage_path, status, job_type) VALUES (:id, :user_id, 'users/job.mp4', 'queued', 'default')"
            ),
            {"id": job_id, "user_id": user_id},
        )
        _assert_rejected(
            connection,
            text("""
                INSERT INTO media_asset_references (id, user_id, asset_id, source_id, job_id)
                VALUES (:id, :user_id, :asset_id, :source_id, :job_id)
            """),
            {
                "id": uuid.uuid4(),
                "user_id": user_id,
                "asset_id": asset_id,
                "source_id": source_id,
                "job_id": job_id,
            },
        )
        _assert_rejected(
            connection,
            text("""
                INSERT INTO media_asset_references (id, user_id, asset_id, job_id)
                VALUES (:id, :user_id, :asset_id, :job_id)
            """),
            {"id": uuid.uuid4(), "user_id": user_id, "asset_id": asset_id, "job_id": job_id},
        )


@pytest.mark.parametrize("asset_wide", [False, True])
def test_reference_writer_serializes_with_retention_owner_lock(asset_wide: bool) -> None:
    with _engine.begin() as connection:
        user_id = _insert_user(connection)
        asset_id = _insert_asset(connection, user_id)
        source_id = _insert_source(connection, user_id, asset_id)
        thread_id = uuid.uuid4()
        connection.execute(
            text("INSERT INTO creation_threads (id, creator_id) VALUES (:id, :owner)"),
            {"id": thread_id, "owner": user_id},
        )
    try:
        with _engine.begin() as holder:
            holder.execute(
                text("SELECT pg_advisory_xact_lock(project_media_reference_lock_key_0103(:owner))"),
                {"owner": user_id},
            )
            with _engine.connect() as writer:
                writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                with pytest.raises(DBAPIError, match="lock timeout"):
                    writer.execute(
                        text(
                            "INSERT INTO media_asset_references (id, user_id, asset_id, source_id, creation_thread_id) VALUES (:id, :owner, :asset, :source, :thread)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "owner": user_id,
                            "asset": asset_id,
                            "source": None if asset_wide else source_id,
                            "thread": thread_id,
                        },
                    )
                writer.rollback()
    finally:
        with _engine.begin() as connection:
            connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
