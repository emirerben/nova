"""Contract tests for the chat-first persistence boundary.

These tests intentionally exercise SQLAlchemy metadata and migration source so
they run in the fast unit suite without requiring a GCS bucket or an LLM.
Database-backed route tests use the normal API integration fixtures.
"""

from pathlib import Path

from app import models


def test_creation_thread_metadata_has_owner_revision_links_and_projection() -> None:
    table = models.Base.metadata.tables["creation_threads"]
    assert {
        "creator_id",
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


def test_creation_thread_relationships_are_owner_scoped() -> None:
    assert models.CreationThread.creator.property.back_populates == "creation_threads"
    assert models.User.creation_threads.property.back_populates == "creator"
    assert models.CreationThread.events.property.back_populates == "thread"
