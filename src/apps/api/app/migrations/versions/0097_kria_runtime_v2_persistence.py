"""Add the durable persistence contract for Kria runtime v2.

The migration is expand-only for production rollout: every existing thread
remains runtime v1 and every execution extension is nullable. Runtime v2 is
selected only when a new thread is created explicitly with version 2.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def _uuid(name: str, *args, **kwargs) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, **kwargs)


def upgrade() -> None:
    op.add_column(
        "creation_threads",
        sa.Column("runtime_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_check_constraint(
        "ck_creation_threads_runtime_version",
        "creation_threads",
        "runtime_version IN (1, 2)",
        postgresql_not_valid=True,
    )
    op.execute(
        """
        CREATE FUNCTION creation_thread_runtime_version_immutable_0097()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.runtime_version IS DISTINCT FROM OLD.runtime_version THEN
                RAISE EXCEPTION 'creation_threads.runtime_version is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER creation_thread_runtime_version_immutable
        BEFORE UPDATE OF runtime_version ON creation_threads
        FOR EACH ROW EXECUTE FUNCTION creation_thread_runtime_version_immutable_0097()
        """
    )

    op.add_column(
        "creation_thread_events",
        # No FK by design. ON DELETE SET NULL would attempt to mutate the
        # database-enforced append-only transcript when a v1 session is erased.
        _uuid("source_agent_event_id", nullable=True),
    )
    op.create_table(
        "creator_agent_turns",
        _uuid("id", server_default=sa.text("gen_random_uuid()"), primary_key=True),
        _uuid(
            "thread_id",
            sa.ForeignKey("creation_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _uuid(
            "session_id",
            sa.ForeignKey("creator_agent_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _uuid(
            "source_event_id",
            sa.ForeignKey("creation_thread_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_event_id", sa.Text(), nullable=False),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("plan_json", postgresql.JSONB(), nullable=True),
        _uuid(
            "observed_event_id",
            sa.ForeignKey("creation_thread_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _uuid("queued_replaces_turn_id", nullable=True),
        sa.Column("cancel_requested_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_epoch", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','queued','planning','executing','awaiting_approval',"
            "'observing','completed','failed','cancelled','superseded')",
            name="ck_creator_agent_turns_status",
        ),
        sa.CheckConstraint("lease_epoch >= 0", name="ck_creator_agent_turns_lease_epoch"),
        sa.UniqueConstraint(
            "thread_id", "client_event_id", name="uq_creator_agent_turns_client_id"
        ),
        sa.UniqueConstraint("source_event_id", name="uq_creator_agent_turns_source_event"),
    )
    op.create_foreign_key(
        "fk_creator_agent_turns_queued_replaces",
        "creator_agent_turns",
        "creator_agent_turns",
        ["queued_replaces_turn_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_creator_agent_turns_thread_created",
        "creator_agent_turns",
        ["thread_id", "created_at", "id"],
    )
    op.create_index(
        "uq_creator_agent_turns_active",
        "creator_agent_turns",
        ["thread_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('planning','executing','observing')"),
    )
    op.create_index(
        "uq_creator_agent_turns_queued",
        "creator_agent_turns",
        ["thread_id"],
        unique=True,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "idx_creator_agent_turns_lease_expiry",
        "creator_agent_turns",
        ["lease_expires_at"],
        postgresql_where=sa.text("lease_expires_at IS NOT NULL"),
    )
    op.create_index(
        "idx_creator_agent_turns_reconcile",
        "creator_agent_turns",
        ["status", "lease_expires_at", "created_at", "id"],
        postgresql_where=sa.text("status IN ('pending','planning')"),
    )

    op.create_table(
        "creator_edit_drafts",
        _uuid("id", server_default=sa.text("gen_random_uuid()"), primary_key=True),
        _uuid(
            "creator_id",
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _uuid(
            "thread_id",
            sa.ForeignKey("creation_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _uuid(
            "item_id",
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("variant_key", sa.Text(), nullable=False),
        _uuid("base_job_id", sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("base_generation_id", sa.Text(), nullable=True),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        _uuid("parent_draft_id", nullable=True),
        sa.Column("snapshot_json", postgresql.JSONB(), nullable=True),
        sa.Column("snapshot_hash", sa.Text(), nullable=False),
        _uuid(
            "source_execution_id",
            sa.ForeignKey("creator_agent_executions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_head", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("draft_revision >= 0", name="ck_creator_edit_drafts_revision"),
        sa.CheckConstraint("length(variant_key) > 0", name="ck_creator_edit_drafts_variant_key"),
        sa.CheckConstraint(
            "snapshot_json IS NULL OR octet_length(snapshot_json::text) <= 2097152",
            name="ck_creator_edit_drafts_snapshot_size",
        ),
        sa.UniqueConstraint(
            "item_id",
            "variant_key",
            "draft_revision",
            name="uq_creator_edit_drafts_revision",
        ),
    )
    op.create_foreign_key(
        "fk_creator_edit_drafts_parent",
        "creator_edit_drafts",
        "creator_edit_drafts",
        ["parent_draft_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_creator_edit_drafts_thread_created",
        "creator_edit_drafts",
        ["thread_id", "created_at"],
    )
    op.create_index(
        "uq_creator_edit_drafts_head",
        "creator_edit_drafts",
        ["item_id", "variant_key"],
        unique=True,
        postgresql_where=sa.text("is_head IS TRUE"),
    )
    op.create_index(
        "idx_creator_edit_drafts_prune",
        "creator_edit_drafts",
        ["created_at", "id"],
        postgresql_where=sa.text("is_head IS FALSE AND snapshot_json IS NOT NULL"),
    )

    op.create_table(
        "creator_agent_approvals",
        _uuid("id", server_default=sa.text("gen_random_uuid()"), primary_key=True),
        _uuid(
            "creator_id",
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _uuid(
            "thread_id",
            sa.ForeignKey("creation_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _uuid(
            "session_id",
            sa.ForeignKey("creator_agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _uuid(
            "turn_id",
            sa.ForeignKey("creator_agent_turns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _uuid(
            "draft_id",
            sa.ForeignKey("creator_edit_drafts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("draft_revision", sa.Integer(), nullable=True),
        _uuid("target_job_id", sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("target_variant_id", sa.Text(), nullable=True),
        sa.Column("target_generation_id", sa.Text(), nullable=True),
        sa.Column("target_manifest_hash", sa.Text(), nullable=True),
        sa.Column("target_ownership_epoch", sa.BigInteger(), nullable=False),
        sa.Column(
            "execution_ids",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("consequence_summary", sa.Text(), nullable=False),
        sa.Column("cost_summary", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','denied','expired','cancelled','consumed')",
            name="ck_creator_agent_approvals_status",
        ),
        sa.CheckConstraint(
            "draft_revision IS NULL OR draft_revision >= 0",
            name="ck_creator_agent_approvals_draft_revision",
        ),
        sa.CheckConstraint(
            "target_ownership_epoch >= 0",
            name="ck_creator_agent_approvals_ownership_epoch",
        ),
    )
    op.create_index(
        "idx_creator_agent_approvals_thread_created",
        "creator_agent_approvals",
        ["thread_id", "created_at"],
    )
    op.create_index(
        "idx_creator_agent_approvals_pending_expiry",
        "creator_agent_approvals",
        ["expires_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_creator_agent_approvals_pending_turn",
        "creator_agent_approvals",
        ["turn_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "idx_creator_agent_approvals_approved_reconcile",
        "creator_agent_approvals",
        ["created_at", "id"],
        postgresql_where=sa.text("status = 'approved'"),
    )

    for column in (
        _uuid("turn_id"),
        sa.Column("tool_name", sa.Text()),
        sa.Column("tool_version", sa.Integer()),
        sa.Column("risk", sa.Text()),
        sa.Column("dependency_group", sa.Integer()),
        sa.Column("group_order", sa.Integer()),
        _uuid("target_thread_id"),
        _uuid("target_draft_id"),
        sa.Column("target_draft_revision", sa.Integer()),
        _uuid("target_job_id"),
        sa.Column("target_variant_id", sa.Text()),
        sa.Column("target_generation_id", sa.Text()),
        sa.Column("target_manifest_hash", sa.Text()),
        sa.Column("target_ownership_epoch", sa.BigInteger()),
        sa.Column("external_task_id", sa.Text()),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("awaiting_approval_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("accepted_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("dispatched_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True)),
        _uuid("observed_event_id"),
    ):
        op.add_column("creator_agent_executions", column)

    for name, local_column, remote_table in (
        ("fk_creator_agent_executions_turn", "turn_id", "creator_agent_turns"),
        (
            "fk_creator_agent_executions_target_thread",
            "target_thread_id",
            "creation_threads",
        ),
        (
            "fk_creator_agent_executions_target_draft",
            "target_draft_id",
            "creator_edit_drafts",
        ),
        ("fk_creator_agent_executions_target_job", "target_job_id", "jobs"),
        (
            "fk_creator_agent_executions_observed_event",
            "observed_event_id",
            "creation_thread_events",
        ),
    ):
        op.create_foreign_key(
            name,
            "creator_agent_executions",
            remote_table,
            [local_column],
            ["id"],
            ondelete="SET NULL",
            postgresql_not_valid=True,
        )

    op.drop_constraint(
        "ck_creator_agent_executions_status", "creator_agent_executions", type_="check"
    )
    op.create_check_constraint(
        "ck_creator_agent_executions_status",
        "creator_agent_executions",
        "status IN ('pending','running','awaiting_approval','accepted','dispatched',"
        "'completed','succeeded','failed','cancelled','stale','duplicate','outcome_unknown')",
        postgresql_not_valid=True,
    )
    op.create_check_constraint(
        "ck_creator_agent_executions_v2_counters",
        "creator_agent_executions",
        "(dependency_group IS NULL OR dependency_group >= 0) AND "
        "(group_order IS NULL OR group_order >= 0) AND "
        "(target_draft_revision IS NULL OR target_draft_revision >= 0) AND "
        "(target_ownership_epoch IS NULL OR target_ownership_epoch >= 0)",
        postgresql_not_valid=True,
    )


def downgrade() -> None:
    # Production rollback is flag-based. Refuse destructive schema rollback
    # once any runtime-v2 durable state has been written.
    op.execute(
        "LOCK TABLE creation_threads, creation_thread_events, creator_agent_turns, "
        "creator_edit_drafts, creator_agent_approvals, creator_agent_executions "
        "IN ACCESS EXCLUSIVE MODE"
    )
    bind = op.get_bind()
    has_v2_state = any(
        bind.scalar(sa.text(statement))
        for statement in (
            "SELECT count(*) FROM creator_agent_turns",
            "SELECT count(*) FROM creator_edit_drafts",
            "SELECT count(*) FROM creator_agent_approvals",
            "SELECT count(*) FROM creation_threads WHERE runtime_version = 2",
            "SELECT count(*) FROM creation_thread_events WHERE source_agent_event_id IS NOT NULL",
            "SELECT count(*) FROM creator_agent_executions "
            "WHERE turn_id IS NOT NULL OR status NOT IN "
            "('pending','running','succeeded','failed','stale','duplicate')",
        )
    )
    if has_v2_state:
        raise RuntimeError(
            "Refusing to downgrade 0097 while Kria runtime-v2 data exists; "
            "use the runtime flag rollback or export and remove v2 state first."
        )

    op.drop_constraint(
        "ck_creator_agent_executions_v2_counters", "creator_agent_executions", type_="check"
    )
    op.drop_constraint(
        "ck_creator_agent_executions_status", "creator_agent_executions", type_="check"
    )
    op.create_check_constraint(
        "ck_creator_agent_executions_status",
        "creator_agent_executions",
        "status IN ('pending','running','succeeded','failed','stale','duplicate')",
    )
    for name in (
        "observed_event_id",
        "observed_at",
        "dispatched_at",
        "accepted_at",
        "awaiting_approval_at",
        "started_at",
        "external_task_id",
        "target_ownership_epoch",
        "target_manifest_hash",
        "target_generation_id",
        "target_variant_id",
        "target_job_id",
        "target_draft_revision",
        "target_draft_id",
        "target_thread_id",
        "group_order",
        "dependency_group",
        "risk",
        "tool_version",
        "tool_name",
        "turn_id",
    ):
        op.drop_column("creator_agent_executions", name)

    op.drop_index("uq_creator_agent_approvals_pending_turn", table_name="creator_agent_approvals")
    op.drop_index(
        "idx_creator_agent_approvals_approved_reconcile",
        table_name="creator_agent_approvals",
    )
    op.drop_index(
        "idx_creator_agent_approvals_pending_expiry", table_name="creator_agent_approvals"
    )
    op.drop_index(
        "idx_creator_agent_approvals_thread_created", table_name="creator_agent_approvals"
    )
    op.drop_table("creator_agent_approvals")
    op.drop_index("uq_creator_edit_drafts_head", table_name="creator_edit_drafts")
    op.drop_index("idx_creator_edit_drafts_prune", table_name="creator_edit_drafts")
    op.drop_index("idx_creator_edit_drafts_thread_created", table_name="creator_edit_drafts")
    op.drop_table("creator_edit_drafts")
    op.drop_index("idx_creator_agent_turns_reconcile", table_name="creator_agent_turns")
    op.drop_index("idx_creator_agent_turns_lease_expiry", table_name="creator_agent_turns")
    op.drop_index("uq_creator_agent_turns_queued", table_name="creator_agent_turns")
    op.drop_index("uq_creator_agent_turns_active", table_name="creator_agent_turns")
    op.drop_index("idx_creator_agent_turns_thread_created", table_name="creator_agent_turns")
    op.drop_table("creator_agent_turns")

    op.drop_column("creation_thread_events", "source_agent_event_id")
    op.execute(
        "DROP TRIGGER IF EXISTS creation_thread_runtime_version_immutable ON creation_threads"
    )
    op.execute("DROP FUNCTION IF EXISTS creation_thread_runtime_version_immutable_0097()")
    op.drop_constraint("ck_creation_threads_runtime_version", "creation_threads", type_="check")
    op.drop_column("creation_threads", "runtime_version")
