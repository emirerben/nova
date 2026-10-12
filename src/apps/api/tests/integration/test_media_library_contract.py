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


def _insert_temporary_upload(
    connection: object,
    user_id: uuid.UUID,
    object_path: str,
    *,
    status: str = "reserved",
    cleanup_claimed_at: datetime | None = None,
) -> None:
    connection.execute(
        text("""
            INSERT INTO temporary_media_uploads
              (id, user_id, object_path, purpose, status, retention_expires_at, cleanup_claimed_at)
            VALUES (:id, :user_id, :object_path, 'test', :status, :expires_at, :cleanup_claimed_at)
        """),
        {
            "id": uuid.uuid4(),
            "user_id": user_id,
            "object_path": object_path,
            "status": status,
            "expires_at": datetime.now(UTC) + timedelta(days=1),
            "cleanup_claimed_at": cleanup_claimed_at,
        },
    )


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


@pytest.mark.parametrize(
    ("status", "cleanup_claimed_at"),
    [
        ("cleanup_pending", None),
        ("deleted", None),
        ("reserved", datetime.now(UTC)),
    ],
)
@pytest.mark.parametrize(
    "path_template",
    [
        "users/{owner}/generative/{upload}/clip.mp4",
        "{owner}/{job}/device/{attempt}.mp4",
    ],
)
def test_source_writer_rejects_cleanup_claimed_temporary_receipts(
    status: str, cleanup_claimed_at: datetime | None, path_template: str
) -> None:
    """A committed cleanup receipt must fence adoption before remote deletion runs.

    Before the 0117 trigger hardening this INSERT succeeds, so rollback leaves
    a new available source pointing at bytes that a cleanup worker may delete.
    """
    with _engine.begin() as connection:
        user_id = _insert_user(connection)
        asset_id = _insert_asset(connection, user_id)
        path = path_template.format(owner=user_id, upload=uuid.uuid4(), job=uuid.uuid4(), attempt=1)
        _insert_temporary_upload(
            connection,
            user_id,
            path,
            status=status,
            cleanup_claimed_at=cleanup_claimed_at,
        )
        _assert_rejected(
            connection,
            text("""
                INSERT INTO media_asset_sources
                  (id, user_id, asset_id, role, object_path, object_generation, byte_count, content_type)
                VALUES (:id, :user_id, :asset_id, 'original', :path, 'g-1', 1, 'video/mp4')
            """),
            {"id": uuid.uuid4(), "user_id": user_id, "asset_id": asset_id, "path": path},
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM media_asset_sources WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            == 0
        )


def test_source_writer_allows_unclaimed_and_adopted_temporary_receipts() -> None:
    """Reserved unclaimed and already-adopted receipts remain valid durable adoption paths."""
    with _engine.begin() as connection:
        user_id = _insert_user(connection)
        asset_id = _insert_asset(connection, user_id)
        for status in ("reserved", "adopted"):
            path = f"users/{user_id}/generative/{uuid.uuid4()}/clip.mp4"
            _insert_temporary_upload(connection, user_id, path, status=status)
            connection.execute(
                text("""
                    INSERT INTO media_asset_sources
                      (id, user_id, asset_id, role, object_path, object_generation, byte_count, content_type)
                    VALUES (:id, :user_id, :asset_id, 'original', :path, :generation, 1, 'video/mp4')
                """),
                {
                    "id": uuid.uuid4(),
                    "user_id": user_id,
                    "asset_id": asset_id,
                    "path": path,
                    "generation": status,
                },
            )


def test_source_writer_rejects_available_transition_after_temporary_cleanup_claim() -> None:
    """A previously missing source cannot become available after its receipt is claimed."""
    with _engine.begin() as connection:
        user_id = _insert_user(connection)
        asset_id = _insert_asset(connection, user_id)
        source_id = uuid.uuid4()
        path = f"users/{user_id}/generative/{uuid.uuid4()}/clip.mp4"
        _insert_temporary_upload(connection, user_id, path)
        connection.execute(
            text("""
                INSERT INTO media_asset_sources
                  (id, user_id, asset_id, role, object_path, object_generation, byte_count, content_type, status)
                VALUES (:id, :user_id, :asset_id, 'original', :path, 'g-1', 1, 'video/mp4', 'missing')
            """),
            {"id": source_id, "user_id": user_id, "asset_id": asset_id, "path": path},
        )
        connection.execute(
            text(
                "UPDATE temporary_media_uploads SET status = 'cleanup_pending' WHERE object_path = :path"
            ),
            {"path": path},
        )
        _assert_rejected(
            connection,
            text("UPDATE media_asset_sources SET status = 'available' WHERE id = :id"),
            {"id": source_id},
        )
        assert (
            connection.scalar(
                text("SELECT status FROM media_asset_sources WHERE id = :id"), {"id": source_id}
            )
            == "missing"
        )


def test_reference_identity_cannot_move_or_narrow_an_existing_pin() -> None:
    """Changing a reference identity must detach then attach, preserving the old pin reset.

    Before the 0117 hardening a source-specific reference can be moved or an
    asset-wide reference narrowed by UPDATE, without running DELETE's source
    inactivity reset for the original pin.
    """
    with _engine.begin() as connection:
        user_id = _insert_user(connection)
        asset_id = _insert_asset(connection, user_id)
        first_source = _insert_source(connection, user_id, asset_id)
        second_source = uuid.uuid4()
        connection.execute(
            text("""
                INSERT INTO media_asset_sources
                  (id, user_id, asset_id, role, object_path, object_generation, byte_count, content_type)
                VALUES (:id, :owner, :asset, 'thumbnail', :path, 'g-2', 1, 'video/mp4')
            """),
            {
                "id": second_source,
                "owner": user_id,
                "asset": asset_id,
                "path": f"users/{user_id}/library/b.mp4",
            },
        )
        job_id = uuid.uuid4()
        reference_id = uuid.uuid4()
        connection.execute(
            text(
                "INSERT INTO jobs (id, user_id, raw_storage_path, status, job_type) VALUES (:id, :owner, 'users/job.mp4', 'queued', 'default')"
            ),
            {"id": job_id, "owner": user_id},
        )
        connection.execute(
            text("""
                INSERT INTO media_asset_references (id, user_id, asset_id, source_id, job_id)
                VALUES (:id, :owner, :asset, :source, :job)
            """),
            {
                "id": reference_id,
                "owner": user_id,
                "asset": asset_id,
                "source": first_source,
                "job": job_id,
            },
        )
        old_activity = datetime.now(UTC) - timedelta(days=100)
        connection.execute(
            text("UPDATE media_asset_sources SET last_used_at = :old WHERE id = :id"),
            {"old": old_activity, "id": first_source},
        )
        _assert_rejected(
            connection,
            text("UPDATE media_asset_references SET source_id = :source WHERE id = :id"),
            {"source": second_source, "id": reference_id},
        )
        assert (
            connection.scalar(
                text("SELECT source_id FROM media_asset_references WHERE id = :id"),
                {"id": reference_id},
            )
            == first_source
        )
        assert (
            connection.scalar(
                text("SELECT last_used_at FROM media_asset_sources WHERE id = :id"),
                {"id": first_source},
            )
            == old_activity
        )
        asset_wide_job_id = uuid.uuid4()
        asset_wide_reference_id = uuid.uuid4()
        connection.execute(
            text(
                "INSERT INTO jobs (id, user_id, raw_storage_path, status, job_type) VALUES (:id, :owner, 'users/job-wide.mp4', 'queued', 'default')"
            ),
            {"id": asset_wide_job_id, "owner": user_id},
        )
        connection.execute(
            text("""
                INSERT INTO media_asset_references (id, user_id, asset_id, job_id)
                VALUES (:id, :owner, :asset, :job)
            """),
            {
                "id": asset_wide_reference_id,
                "owner": user_id,
                "asset": asset_id,
                "job": asset_wide_job_id,
            },
        )
        _assert_rejected(
            connection,
            text("UPDATE media_asset_references SET source_id = :source WHERE id = :id"),
            {"source": first_source, "id": asset_wide_reference_id},
        )
        assert (
            connection.scalar(
                text("SELECT source_id FROM media_asset_references WHERE id = :id"),
                {"id": asset_wide_reference_id},
            )
            is None
        )
        connection.execute(
            text("UPDATE media_asset_references SET created_at = created_at WHERE id = :id"),
            {"id": reference_id},
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
        connection.execute(
            text("""
                UPDATE storage_retention_manifests
                SET status = 'completed', executed_at = :now
                WHERE id = :id
            """),
            {"id": manifest_id, "now": datetime.now(UTC)},
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
