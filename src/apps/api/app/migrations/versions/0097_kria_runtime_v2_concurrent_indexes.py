"""Build Kria indexes on continuously written tables without blocking writes.

Revision ID: 0097
Revises: 0096
Create Date: 2026-09-07
"""

from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


_INDEXES = (
    (
        "uq_creation_thread_events_source_agent_event",
        "CREATE UNIQUE INDEX CONCURRENTLY "
        "uq_creation_thread_events_source_agent_event "
        "ON creation_thread_events (thread_id, source_agent_event_id) "
        "WHERE source_agent_event_id IS NOT NULL",
    ),
    (
        "idx_creator_agent_executions_turn_order",
        "CREATE INDEX CONCURRENTLY idx_creator_agent_executions_turn_order "
        "ON creator_agent_executions (turn_id, dependency_group, group_order)",
    ),
    (
        "idx_creator_agent_executions_pending_dispatch",
        "CREATE INDEX CONCURRENTLY idx_creator_agent_executions_pending_dispatch "
        "ON creator_agent_executions (accepted_at) WHERE status = 'accepted'",
    ),
    (
        "idx_creator_agent_executions_dispatched_observe",
        "CREATE INDEX CONCURRENTLY idx_creator_agent_executions_dispatched_observe "
        "ON creator_agent_executions (dispatched_at, id) "
        "WHERE status = 'dispatched' AND target_job_id IS NOT NULL",
    ),
    (
        "idx_creator_agent_executions_target_job_created",
        "CREATE INDEX CONCURRENTLY idx_creator_agent_executions_target_job_created "
        "ON creator_agent_executions (target_job_id, created_at) "
        "WHERE turn_id IS NOT NULL AND target_job_id IS NOT NULL",
    ),
)


def upgrade() -> None:
    # A failed concurrent build leaves an invalid same-name index. Drop every
    # candidate first so a migration retry can never accept that artifact.
    with op.get_context().autocommit_block():
        for name, statement in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(statement)
    # VALIDATE takes a lighter lock than installing a validated constraint and
    # does not block normal reads/writes while PostgreSQL scans legacy rows.
    op.execute(
        "ALTER TABLE creation_threads VALIDATE CONSTRAINT ck_creation_threads_runtime_version"
    )
    op.execute(
        "ALTER TABLE creator_agent_executions "
        "VALIDATE CONSTRAINT ck_creator_agent_executions_status"
    )
    op.execute(
        "ALTER TABLE creator_agent_executions "
        "VALIDATE CONSTRAINT ck_creator_agent_executions_v2_counters"
    )
    for constraint in (
        "fk_creator_agent_executions_turn",
        "fk_creator_agent_executions_target_thread",
        "fk_creator_agent_executions_target_draft",
        "fk_creator_agent_executions_target_job",
        "fk_creator_agent_executions_observed_event",
    ):
        op.execute(f"ALTER TABLE creator_agent_executions VALIDATE CONSTRAINT {constraint}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _statement in reversed(_INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
