"""Structural guards for the Main Creator Agent persistence foundation."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlalchemy.orm import configure_mappers

from app import models


def test_creator_agent_tables_and_relationships_are_registered() -> None:
    configure_mappers()
    tables = models.Base.metadata.tables
    assert {
        "creator_agent_sessions",
        "creator_agent_events",
        "creator_agent_executions",
    } <= set(tables)
    assert "creator_agent_session_id" in tables["agent_run"].columns
    assert {
        "status",
        "active_plan",
        "manifest_hash",
        "target_job_id",
        "target_variant_id",
        "target_generation_id",
        "max_render_attempts",
        "render_attempts",
        "last_review",
        "last_good",
        "last_error",
    } <= set(tables["creator_agent_sessions"].columns.keys())
    assert {"role", "sequence", "client_event_id"} <= set(
        tables["creator_agent_events"].columns.keys()
    )

    assert any(
        fk.column.table.name == "users"
        for fk in tables["creator_agent_sessions"].c.creator_id.foreign_keys
    )
    assert any(
        fk.column.table.name == "plan_items"
        for fk in tables["creator_agent_sessions"].c.plan_item_id.foreign_keys
    )
    assert any(
        fk.column.table.name == "jobs"
        for fk in tables["creator_agent_sessions"].c.target_job_id.foreign_keys
    )
    assert any(
        fk.column.table.name == "creator_agent_sessions"
        for fk in tables["agent_run"].c.creator_agent_session_id.foreign_keys
    )


def test_active_session_and_event_execution_idempotency_constraints() -> None:
    sessions = models.Base.metadata.tables["creator_agent_sessions"]
    events = models.Base.metadata.tables["creator_agent_events"]
    executions = models.Base.metadata.tables["creator_agent_executions"]

    active = next(
        index for index in sessions.indexes if index.name == "uq_creator_agent_sessions_active_item"
    )
    assert active.unique is True
    assert "status IN" in str(active.dialect_options["postgresql"]["where"])

    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_creator_agent_events_sequence"
        for constraint in events.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_creator_agent_events_client_id"
        for constraint in events.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_creator_agent_executions_idempotency"
        for constraint in executions.constraints
    )
    assert any(
        isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_creator_agent_executions_status"
        for constraint in executions.constraints
    )


def test_agent_run_session_fk_and_session_children_cascade() -> None:
    agent_run = models.Base.metadata.tables["agent_run"]
    assert agent_run.c.creator_agent_session_id.nullable is True
    session_fk = next(iter(agent_run.c.creator_agent_session_id.foreign_keys))
    assert session_fk.ondelete == "CASCADE"
    owner_constraint = next(
        constraint
        for constraint in agent_run.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == "ck_agent_run_has_owner"
    )
    assert "creator_agent_session_id IS NOT NULL" in str(owner_constraint.sqltext)

    sessions = models.Base.metadata.tables["creator_agent_sessions"]
    assert any(
        isinstance(index, Index) and index.name == "idx_creator_agent_sessions_creator_id"
        for index in sessions.indexes
    )

    for table_name in ("creator_agent_events", "creator_agent_executions"):
        table = models.Base.metadata.tables[table_name]
        fk = next(iter(table.c.session_id.foreign_keys))
        assert fk.ondelete == "CASCADE"


def test_migration_downgrade_removes_session_only_runs_before_old_owner_check() -> None:
    migration = (
        Path(__file__).parents[1]
        / "app"
        / "migrations"
        / "versions"
        / "0081_creator_agent_sessions.py"
    ).read_text()
    delete_at = migration.index("DELETE FROM agent_run WHERE creator_agent_session_id")
    drop_column_at = migration.index('op.drop_column("agent_run", "creator_agent_session_id")')
    recreate_check_at = migration.rindex(
        'op.create_check_constraint(\n        "ck_agent_run_has_owner"'
    )
    assert delete_at < drop_column_at < recreate_check_at
