"""Add durable Main Creator Agent session state and execution receipts.

The session row is the mutable controller state.  Events are append-only and
executions are idempotent receipts, so retries and stale workers cannot be
mistaken for a new creative decision.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def _uuid(name: str, **kwargs):
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        **kwargs,
    )


_ACTIVE_STATUSES = (
    "'briefing','planning','awaiting_confirmation','executing','rendering',"
    "'reviewing','awaiting_feedback','revising'"
)


def upgrade() -> None:
    op.create_table(
        "creator_agent_sessions",
        _uuid("id", primary_key=True),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="briefing", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ownership_epoch", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("active_plan", postgresql.JSONB(), nullable=True),
        sa.Column("manifest_hash", sa.Text(), nullable=True),
        sa.Column(
            "target_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("target_variant_id", sa.Text(), nullable=True),
        sa.Column("target_generation_id", sa.Text(), nullable=True),
        sa.Column("max_render_attempts", sa.Integer(), server_default="2", nullable=False),
        sa.Column("render_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("iteration_budget", sa.Integer(), server_default="2", nullable=False),
        sa.Column("question_budget", sa.Integer(), server_default="1", nullable=False),
        sa.Column("agent_call_budget", sa.Integer(), server_default="8", nullable=False),
        sa.Column("iteration_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("question_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("agent_call_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_review", postgresql.JSONB(), nullable=True),
        sa.Column("last_good", postgresql.JSONB(), nullable=True),
        sa.Column("last_error", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('briefing','planning','awaiting_confirmation','executing','rendering',"
            "'reviewing','awaiting_feedback','revising','completed','failed','cancelled')",
            name="ck_creator_agent_sessions_status",
        ),
        sa.CheckConstraint(
            "revision >= 0 AND ownership_epoch >= 0 AND iteration_budget >= 0 "
            "AND question_budget >= 0 AND agent_call_budget >= 0 "
            "AND iteration_count >= 0 AND question_count >= 0 AND agent_call_count >= 0 "
            "AND max_render_attempts >= 0 AND render_attempts >= 0",
            name="ck_creator_agent_sessions_counters_nonnegative",
        ),
    )
    op.create_index(
        "uq_creator_agent_sessions_active_item",
        "creator_agent_sessions",
        ["creator_id", "plan_item_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({_ACTIVE_STATUSES})"),
    )
    op.create_index(
        "idx_creator_agent_sessions_item_updated",
        "creator_agent_sessions",
        ["plan_item_id", "updated_at"],
    )
    op.create_index(
        "idx_creator_agent_sessions_creator_id",
        "creator_agent_sessions",
        ["creator_id"],
    )
    op.create_index(
        "idx_creator_agent_sessions_target_job", "creator_agent_sessions", ["target_job_id"]
    )

    op.create_table(
        "creator_agent_events",
        _uuid("id", primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creator_agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("client_event_id", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), server_default="system", nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "sequence >= 0 AND revision >= 0", name="ck_creator_agent_events_counters"
        ),
        sa.CheckConstraint(
            "role IN ('user','assistant','system')", name="ck_creator_agent_events_role"
        ),
        sa.UniqueConstraint("session_id", "sequence", name="uq_creator_agent_events_sequence"),
        sa.UniqueConstraint(
            "session_id", "client_event_id", name="uq_creator_agent_events_client_id"
        ),
    )
    op.create_index(
        "idx_creator_agent_events_session_created",
        "creator_agent_events",
        ["session_id", "created_at"],
    )

    op.create_table(
        "creator_agent_executions",
        _uuid("id", primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creator_agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("expected_manifest_hash", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed','stale','duplicate')",
            name="ck_creator_agent_executions_status",
        ),
        sa.CheckConstraint("expected_revision >= 0", name="ck_creator_agent_executions_revision"),
        sa.UniqueConstraint(
            "session_id", "idempotency_key", name="uq_creator_agent_executions_idempotency"
        ),
    )
    op.create_index(
        "idx_creator_agent_executions_session_created",
        "creator_agent_executions",
        ["session_id", "created_at"],
    )

    op.add_column(
        "agent_run",
        sa.Column(
            "creator_agent_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creator_agent_sessions.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.drop_constraint("ck_agent_run_has_owner", "agent_run", type_="check")
    op.create_check_constraint(
        "ck_agent_run_has_owner",
        "agent_run",
        "(job_id IS NOT NULL) "
        "OR (template_id IS NOT NULL) "
        "OR (music_track_id IS NOT NULL) "
        "OR (creator_agent_session_id IS NOT NULL)",
    )
    op.create_index(
        "idx_agent_run_creator_agent_session_created",
        "agent_run",
        ["creator_agent_session_id", "created_at"],
    )

    # Events are audit evidence.  Application code never updates them; this
    # trigger also protects against accidental bulk updates in maintenance
    # scripts or an over-broad ORM flush.
    op.execute(
        """
        CREATE FUNCTION reject_creator_agent_event_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'creator_agent_events are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER creator_agent_events_append_only
        BEFORE UPDATE ON creator_agent_events
        FOR EACH ROW EXECUTE FUNCTION reject_creator_agent_event_update()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS creator_agent_events_append_only ON creator_agent_events")
    op.execute("DROP FUNCTION IF EXISTS reject_creator_agent_event_update()")
    op.drop_index("idx_agent_run_creator_agent_session_created", table_name="agent_run")
    op.drop_constraint("ck_agent_run_has_owner", "agent_run", type_="check")
    # Session-only audit rows cannot satisfy the pre-0081 owner invariant once
    # the session owner column is removed. Downgrading the feature deliberately
    # removes those feature-owned audit rows while preserving multi-owned runs.
    op.execute(
        "DELETE FROM agent_run WHERE creator_agent_session_id IS NOT NULL "
        "AND job_id IS NULL AND template_id IS NULL AND music_track_id IS NULL"
    )
    op.drop_column("agent_run", "creator_agent_session_id")
    op.create_check_constraint(
        "ck_agent_run_has_owner",
        "agent_run",
        "(job_id IS NOT NULL) OR (template_id IS NOT NULL) OR (music_track_id IS NOT NULL)",
    )
    op.drop_index(
        "idx_creator_agent_executions_session_created", table_name="creator_agent_executions"
    )
    op.drop_table("creator_agent_executions")
    op.drop_index("idx_creator_agent_events_session_created", table_name="creator_agent_events")
    op.drop_table("creator_agent_events")
    op.drop_index("idx_creator_agent_sessions_target_job", table_name="creator_agent_sessions")
    op.drop_index("idx_creator_agent_sessions_creator_id", table_name="creator_agent_sessions")
    op.drop_index("idx_creator_agent_sessions_item_updated", table_name="creator_agent_sessions")
    op.drop_index("uq_creator_agent_sessions_active_item", table_name="creator_agent_sessions")
    op.drop_table("creator_agent_sessions")
