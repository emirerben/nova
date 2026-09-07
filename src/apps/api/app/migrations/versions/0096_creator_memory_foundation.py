# ruff: noqa: E501
"""Add the account-wide creator direction ledger foundation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def _uuid(name: str, *, fk: str | None = None, ondelete: str | None = None):
    constraints = (sa.ForeignKey(fk, ondelete=ondelete),) if fk else ()
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        *constraints,
        nullable=False,
        primary_key=name == "id",
    )


def upgrade() -> None:
    op.add_column(
        "content_plans",
        sa.Column(
            "creator_direction_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.add_column(
        "creation_threads",
        sa.Column(
            "creator_direction_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.add_column(
        "users",
        sa.Column("creator_memory_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("creator_memory_revision", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "ck_users_creator_memory_revision", "users", "creator_memory_revision >= 0"
    )

    op.create_table(
        "creator_memory_items",
        _uuid("id"),
        _uuid("user_id", fk="users.id", ondelete="CASCADE"),
        sa.Column("scope_kind", sa.Text(), server_default="account", nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("normalized_key", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("enforcement", sa.Text(), nullable=False),
        sa.Column("structured_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_kind", sa.Text(), server_default="profile", nullable=False),
        sa.Column(
            "source_thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creation_threads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creation_thread_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("state", sa.Text(), server_default="active", nullable=False),
        sa.Column("user_locked", sa.Boolean(), server_default=sa.false(), nullable=False),
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
        sa.CheckConstraint("scope_kind = 'account'", name="ck_creator_memory_scope_kind"),
        sa.CheckConstraint(
            "enforcement IN ('constraint','default','advisory')",
            name="ck_creator_memory_enforcement",
        ),
        sa.CheckConstraint(
            "source_kind IN ('profile','creation_thread')", name="ck_creator_memory_source_kind"
        ),
        sa.CheckConstraint(
            "state IN ('active','suggested','dismissed','superseded','forgotten')",
            name="ck_creator_memory_state",
        ),
        sa.CheckConstraint(
            "length(instruction) BETWEEN 1 AND 500", name="ck_creator_memory_instruction"
        ),
    )
    op.create_index(
        "idx_creator_memory_items_user_state_updated",
        "creator_memory_items",
        ["user_id", "state", sa.text("updated_at DESC")],
    )
    op.create_index(
        "idx_creator_memory_items_source_event", "creator_memory_items", ["source_event_id"]
    )
    op.create_index(
        "idx_creator_memory_items_source_thread", "creator_memory_items", ["source_thread_id"]
    )
    op.create_index(
        "uq_creator_memory_items_active_key",
        "creator_memory_items",
        ["user_id", "scope_kind", "normalized_key"],
        unique=True,
        postgresql_where=sa.text("state = 'active' AND normalized_key IS NOT NULL"),
    )
    op.create_index(
        "uq_creator_memory_items_advisory_hash",
        "creator_memory_items",
        ["user_id", "scope_kind", "content_hash"],
        unique=True,
        postgresql_where=sa.text("content_hash IS NOT NULL AND state IN ('active','suggested')"),
    )

    op.create_table(
        "creator_memory_operations",
        _uuid("id"),
        _uuid("user_id", fk="users.id", ondelete="CASCADE"),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("operation_kind", sa.Text(), nullable=False),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creator_memory_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("prior_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resulting_revision", sa.BigInteger(), nullable=False),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column(
            "source_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creation_thread_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("undo_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("undone_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name="uq_creator_memory_operation_idempotency"
        ),
    )
    op.create_index(
        "idx_creator_memory_operations_user_created",
        "creator_memory_operations",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index("idx_creator_memory_operations_item", "creator_memory_operations", ["item_id"])
    op.create_index(
        "idx_creator_memory_operations_source_event",
        "creator_memory_operations",
        ["source_event_id"],
    )

    op.create_table(
        "creator_memory_outbox",
        _uuid("id"),
        _uuid("user_id", fk="users.id", ondelete="CASCADE"),
        sa.Column(
            "source_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creation_thread_events.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("source_message", sa.Text(), nullable=False),
        sa.Column("payload_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("result_code", sa.Text(), nullable=True),
        sa.Column("extractor_version", sa.Text(), nullable=True),
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
            "status IN ('pending','leased','succeeded','dead')",
            name="ck_creator_memory_outbox_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_creator_memory_outbox_attempts"),
        sa.UniqueConstraint("source_event_id", name="uq_creator_memory_outbox_source_event"),
        sa.CheckConstraint(
            "length(source_message) BETWEEN 1 AND 2000",
            name="ck_creator_memory_outbox_source_message",
        ),
    )
    op.create_index("idx_creator_memory_outbox_user", "creator_memory_outbox", ["user_id"])
    op.create_index(
        "idx_creator_memory_outbox_pending_claim",
        "creator_memory_outbox",
        ["available_at", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "idx_creator_memory_outbox_leased_claim",
        "creator_memory_outbox",
        ["lease_until", "available_at", "created_at"],
        postgresql_where=sa.text("status = 'leased'"),
    )

    op.create_table(
        "project_direction_overrides",
        _uuid("id"),
        _uuid("user_id", fk="users.id", ondelete="CASCADE"),
        _uuid("thread_id", fk="creation_threads.id", ondelete="CASCADE"),
        sa.Column("normalized_key", sa.Text(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("structured_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.UniqueConstraint(
            "user_id", "thread_id", "normalized_key", name="uq_project_direction_override_key"
        ),
    )
    op.create_index(
        "idx_project_direction_override_thread", "project_direction_overrides", ["thread_id"]
    )


def downgrade() -> None:
    raise RuntimeError(
        "0096 is intentionally data-preserving; roll back creator memory with feature flags, "
        "not a destructive schema downgrade"
    )
