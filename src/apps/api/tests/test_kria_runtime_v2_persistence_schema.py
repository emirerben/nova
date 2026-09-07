"""Structural guards for the additive Kria runtime-v2 persistence contract."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, Index, Integer, UniqueConstraint
from sqlalchemy.orm import configure_mappers

from app import models


def _constraint_names(table_name: str) -> set[str | None]:
    return {constraint.name for constraint in models.Base.metadata.tables[table_name].constraints}


def _index(table_name: str, name: str) -> Index:
    return next(
        index for index in models.Base.metadata.tables[table_name].indexes if index.name == name
    )


def test_runtime_version_is_additive_and_database_immutable() -> None:
    configure_mappers()
    threads = models.Base.metadata.tables["creation_threads"]
    runtime_version = threads.c.runtime_version
    assert runtime_version.nullable is False
    assert runtime_version.server_default is not None
    assert str(runtime_version.server_default.arg) == "1"
    assert "ck_creation_threads_runtime_version" in _constraint_names("creation_threads")

    migration = (
        Path(__file__).parents[1] / "app/migrations/versions/0096_kria_runtime_v2_persistence.py"
    ).read_text()
    assert 'down_revision = "0095"' in migration
    assert "creation_thread_runtime_version_immutable" in migration
    assert "BEFORE UPDATE OF runtime_version" in migration
    assert "runtime_version IS DISTINCT FROM OLD.runtime_version" in migration
    assert migration.count("postgresql_not_valid=True") == 4


def test_legacy_agent_event_projection_has_per_thread_dedupe() -> None:
    events = models.Base.metadata.tables["creation_thread_events"]
    source = events.c.source_agent_event_id
    assert source.nullable is True
    assert not source.foreign_keys
    index = _index("creation_thread_events", "uq_creation_thread_events_source_agent_event")
    assert index.unique is True
    assert [column.name for column in index.columns] == ["thread_id", "source_agent_event_id"]
    assert "source_agent_event_id IS NOT NULL" in str(index.dialect_options["postgresql"]["where"])


def test_turn_schema_guards_replay_leasing_and_single_successor() -> None:
    turns = models.Base.metadata.tables["creator_agent_turns"]
    assert {
        "thread_id",
        "session_id",
        "source_event_id",
        "client_event_id",
        "request_digest",
        "status",
        "plan_json",
        "observed_event_id",
        "queued_replaces_turn_id",
        "cancel_requested_at",
        "lease_owner",
        "lease_epoch",
        "lease_expires_at",
        "error",
        "completed_at",
    } <= set(turns.columns.keys())
    assert "uq_creator_agent_turns_client_id" in _constraint_names("creator_agent_turns")
    assert "uq_creator_agent_turns_source_event" in _constraint_names("creator_agent_turns")
    assert "ck_creator_agent_turns_status" in _constraint_names("creator_agent_turns")
    assert "ck_creator_agent_turns_lease_epoch" in _constraint_names("creator_agent_turns")

    active = _index("creator_agent_turns", "uq_creator_agent_turns_active")
    queued = _index("creator_agent_turns", "uq_creator_agent_turns_queued")
    assert active.unique is True
    assert queued.unique is True
    assert "planning" in str(active.dialect_options["postgresql"]["where"])
    assert "status = 'queued'" in str(queued.dialect_options["postgresql"]["where"])


def test_draft_schema_guards_revision_and_single_head() -> None:
    drafts = models.Base.metadata.tables["creator_edit_drafts"]
    assert {
        "creator_id",
        "thread_id",
        "item_id",
        "variant_key",
        "base_job_id",
        "base_generation_id",
        "draft_revision",
        "parent_draft_id",
        "snapshot_json",
        "snapshot_hash",
        "source_execution_id",
        "is_head",
    } <= set(drafts.columns.keys())
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_creator_edit_drafts_revision"
        for constraint in drafts.constraints
    )
    assert "ck_creator_edit_drafts_snapshot_size" in _constraint_names("creator_edit_drafts")
    head = _index("creator_edit_drafts", "uq_creator_edit_drafts_head")
    assert head.unique is True
    assert [column.name for column in head.columns] == ["item_id", "variant_key"]
    assert "is_head IS TRUE" in str(head.dialect_options["postgresql"]["where"])
    prune = _index("creator_edit_drafts", "idx_creator_edit_drafts_prune")
    assert [column.name for column in prune.columns] == ["created_at", "id"]
    assert "snapshot_json IS NOT NULL" in str(prune.dialect_options["postgresql"]["where"])


def test_approval_schema_pins_exact_state_and_expiry() -> None:
    approvals = models.Base.metadata.tables["creator_agent_approvals"]
    assert {
        "creator_id",
        "thread_id",
        "session_id",
        "turn_id",
        "draft_id",
        "draft_revision",
        "target_job_id",
        "target_variant_id",
        "target_generation_id",
        "target_manifest_hash",
        "target_ownership_epoch",
        "execution_ids",
        "consequence_summary",
        "cost_summary",
        "status",
        "expires_at",
        "consumed_at",
    } <= set(approvals.columns.keys())
    assert "ck_creator_agent_approvals_status" in _constraint_names("creator_agent_approvals")
    assert "ck_creator_agent_approvals_ownership_epoch" in _constraint_names(
        "creator_agent_approvals"
    )
    pending = _index("creator_agent_approvals", "idx_creator_agent_approvals_pending_expiry")
    assert "status = 'pending'" in str(pending.dialect_options["postgresql"]["where"])
    assert _index("creator_agent_approvals", "uq_creator_agent_approvals_pending_turn").unique
    approved = _index("creator_agent_approvals", "idx_creator_agent_approvals_approved_reconcile")
    assert [column.name for column in approved.columns] == ["created_at", "id"]
    assert "status = 'approved'" in str(approved.dialect_options["postgresql"]["where"])


def test_execution_schema_preserves_v1_and_distinguishes_v2_outcomes() -> None:
    executions = models.Base.metadata.tables["creator_agent_executions"]
    assert {
        "turn_id",
        "tool_name",
        "tool_version",
        "risk",
        "dependency_group",
        "group_order",
        "target_thread_id",
        "target_draft_id",
        "target_draft_revision",
        "target_job_id",
        "target_variant_id",
        "target_generation_id",
        "target_manifest_hash",
        "target_ownership_epoch",
        "external_task_id",
        "started_at",
        "awaiting_approval_at",
        "accepted_at",
        "dispatched_at",
        "completed_at",
        "observed_at",
        "observed_event_id",
    } <= set(executions.columns.keys())
    assert isinstance(executions.c.tool_version.type, Integer)
    status = next(
        constraint
        for constraint in executions.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_creator_agent_executions_status"
    )
    status_sql = str(status.sqltext)
    for required in (
        "succeeded",  # runtime-v1 remains valid
        "awaiting_approval",
        "accepted",
        "dispatched",
        "completed",
        "cancelled",
        "outcome_unknown",
    ):
        assert required in status_sql
    assert "idx_creator_agent_executions_turn_order" in {index.name for index in executions.indexes}
    assert "idx_creator_agent_executions_pending_dispatch" in {
        index.name for index in executions.indexes
    }
    assert [
        column.name
        for column in _index(
            "creator_agent_executions", "idx_creator_agent_executions_dispatched_observe"
        ).columns
    ] == ["dispatched_at", "id"]
    assert [
        column.name
        for column in _index(
            "creator_agent_executions", "idx_creator_agent_executions_target_job_created"
        ).columns
    ] == ["target_job_id", "created_at"]


def test_existing_table_indexes_are_built_concurrently_in_followup_migration() -> None:
    migration = (
        Path(__file__).parents[1]
        / "app/migrations/versions/0097_kria_runtime_v2_concurrent_indexes.py"
    ).read_text()
    assert 'down_revision = "0096"' in migration
    assert migration.count("CREATE INDEX CONCURRENTLY") == 4
    assert migration.count("CREATE UNIQUE INDEX CONCURRENTLY") == 1
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in migration
    assert migration.count("VALIDATE CONSTRAINT") == 4
    for constraint in (
        "ck_creation_threads_runtime_version",
        "ck_creator_agent_executions_status",
        "ck_creator_agent_executions_v2_counters",
        "fk_creator_agent_executions_turn",
        "fk_creator_agent_executions_target_thread",
        "fk_creator_agent_executions_target_draft",
        "fk_creator_agent_executions_target_job",
        "fk_creator_agent_executions_observed_event",
    ):
        assert constraint in migration


def test_migration_refuses_destructive_downgrade_with_v2_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("app.migrations.versions.0096_kria_runtime_v2_persistence")
    statements: list[str] = []

    class Bind:
        def scalar(self, _statement) -> int:
            return 1

    monkeypatch.setattr(migration.op, "execute", statements.append)
    monkeypatch.setattr(migration.op, "get_bind", Bind)

    with pytest.raises(RuntimeError, match="Kria runtime-v2 data exists"):
        migration.downgrade()

    assert statements
    assert "IN ACCESS EXCLUSIVE MODE" in statements[0]
