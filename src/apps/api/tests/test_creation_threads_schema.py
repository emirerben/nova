"""Contract tests for the chat-first persistence boundary.

These tests intentionally exercise SQLAlchemy metadata and migration source so
they run in the fast unit suite without requiring a GCS bucket or an LLM.
Database-backed route tests use the normal API integration fixtures.
"""

import importlib
from pathlib import Path

import pytest

from app import models


def test_creation_thread_metadata_has_owner_revision_links_and_projection() -> None:
    table = models.Base.metadata.tables["creation_threads"]
    assert {
        "creator_id",
        "title",
        "revision",
        "state",
        "content_plan_id",
        "active_plan_item_id",
        "active_job_id",
        "active_creator_agent_session_id",
    } <= set(table.columns.keys())
    assert "uq_creation_threads_active_plan_item" in {
        constraint.name for constraint in table.constraints
    }
    assert "ck_creation_threads_status" in {constraint.name for constraint in table.constraints}
    assert "ck_creation_threads_title_length" in {
        constraint.name for constraint in table.constraints
    }
    tombstones = models.Base.metadata.tables["creation_thread_deletions"]
    assert {"thread_id", "creator_id", "created_at"} <= set(tombstones.columns.keys())
    assert "idx_creation_thread_deletions_creator" in {index.name for index in tombstones.indexes}
    reservations = models.Base.metadata.tables["creation_thread_upload_reservations"]
    assert {
        "id",
        "thread_id",
        "creator_id",
        "media_id",
        "object_path",
        "expires_at",
        "created_at",
    } <= set(reservations.columns.keys())
    assert "uq_creation_thread_upload_media" in {
        constraint.name for constraint in reservations.constraints
    }
    assert "idx_creation_thread_upload_expiry" in {index.name for index in reservations.indexes}
    deletion_manifest = models.Base.metadata.tables["job_storage_deletions"]
    assert "object_prefixes" in deletion_manifest.columns


def test_creation_thread_events_are_ordered_idempotent_and_append_only() -> None:
    table = models.Base.metadata.tables["creation_thread_events"]
    assert {"thread_id", "sequence", "revision", "client_event_id", "role", "payload"} <= set(
        table.columns.keys()
    )
    constraints = {constraint.name for constraint in table.constraints}
    assert "uq_creation_thread_events_sequence" in constraints
    assert "uq_creation_thread_events_revision" in constraints
    assert "uq_creation_thread_events_client_id" in constraints
    assert "ck_creation_thread_events_role" in constraints
    assert "idx_creation_thread_events_client_created" in {index.name for index in table.indexes}
    migration = Path(__file__).parents[1] / "app/migrations/versions/0092_creation_threads.py"
    source = migration.read_text()
    assert "creation_thread_events_append_only" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "BEFORE TRUNCATE ON creation_thread_events" in source
    assert "NOT EXISTS" in source
    assert "creation_threads WHERE id = OLD.thread_id" in source
    assert "jsonb_build_object" in source
    assert "md5(pi.voiceover_gcs_path)" in source
    assert "CASE WHEN pi.voiceover_gcs_path IS NOT NULL THEN 1 ELSE 0 END" in source
    assert "LOCK TABLE creation_threads, creation_thread_events" in source
    assert "IN ACCESS EXCLUSIVE MODE" in source
    assert "Refusing to downgrade 0092 while creation thread data exists" in source
    assert "'gcs_path'" not in source
    title_migration = (
        Path(__file__).parents[1]
        / "app/migrations/versions/0093_creation_thread_titles_deletions.py"
    )
    title_source = title_migration.read_text()
    assert "creation_thread_deletions" in title_source
    assert "creation_thread_upload_reservations" in title_source
    assert "uq_creation_thread_upload_media" in title_source
    assert "idx_creation_thread_upload_expiry" in title_source
    assert '"object_prefixes"' in title_source
    assert "state ->> 'intent'" in title_source


def test_creation_thread_relationships_are_owner_scoped() -> None:
    assert models.CreationThread.creator.property.back_populates == "creation_threads"
    assert models.User.creation_threads.property.back_populates == "creator"


def test_creation_thread_migration_downgrade_refuses_nonempty_lifecycle_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module(
        "app.migrations.versions.0093_creation_thread_titles_deletions"
    )
    lock_statements: list[str] = []

    class Bind:
        def __init__(self) -> None:
            self.counts = iter([0, 0, 0, 1])

        def scalar(self, _statement):
            return next(self.counts)

    monkeypatch.setattr(
        migration.op, "execute", lambda statement: lock_statements.append(statement)
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: Bind())

    with pytest.raises(RuntimeError, match="Refusing to downgrade 0093"):
        migration.downgrade()

    assert lock_statements
    assert "IN ACCESS EXCLUSIVE MODE" in lock_statements[0]
    assert models.CreationThread.events.property.back_populates == "thread"
