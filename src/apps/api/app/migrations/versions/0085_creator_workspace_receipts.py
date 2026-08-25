"""Persist plan-level creator workspace coordination receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "creator_workspace_receipts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ownership_epoch", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('pending','processing','ready','failed','stale')",
            name="ck_creator_workspace_receipts_status",
        ),
        sa.CheckConstraint("ownership_epoch >= 0", name="ck_creator_workspace_receipts_epoch"),
        sa.UniqueConstraint(
            "creator_id",
            "plan_id",
            "idempotency_key",
            name="uq_creator_workspace_receipts_idempotency",
        ),
    )
    op.create_index(
        "idx_creator_workspace_receipts_plan_created",
        "creator_workspace_receipts",
        ["plan_id", "created_at"],
    )

    op.create_table(
        "creator_workspace_deliverables",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "receipt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creator_workspace_receipts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "creator_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creator_agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ownership_epoch", sa.BigInteger(), nullable=False),
        sa.Column("session_revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("variant_id", sa.Text(), nullable=True),
        sa.Column("render_generation_id", sa.Text(), nullable=True),
        sa.Column("generation_receipt", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('pending','processing','ready','failed','stale')",
            name="ck_creator_workspace_deliverables_status",
        ),
        sa.CheckConstraint(
            "ownership_epoch >= 0 AND session_revision >= 0 AND position >= 0",
            name="ck_creator_workspace_deliverables_counters",
        ),
        sa.UniqueConstraint("receipt_id", "plan_item_id", name="uq_creator_workspace_receipt_item"),
        sa.UniqueConstraint("receipt_id", "position", name="uq_creator_workspace_receipt_position"),
    )
    op.create_index(
        "idx_creator_workspace_deliverables_item",
        "creator_workspace_deliverables",
        ["plan_item_id", "created_at"],
    )

    op.create_table(
        "creator_workspace_preference_signals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "receipt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creator_workspace_receipts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ownership_epoch", sa.BigInteger(), nullable=False),
        sa.Column("client_event_id", sa.Text(), nullable=False),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), server_default="creator_explicit", nullable=False),
        sa.Column("signal", sa.Text(), server_default="note", nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("style_edit", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint("source = 'creator_explicit'", name="ck_creator_workspace_pref_source"),
        sa.CheckConstraint("signal = 'note'", name="ck_creator_workspace_pref_signal"),
        sa.CheckConstraint("ownership_epoch >= 0", name="ck_creator_workspace_pref_epoch"),
        sa.UniqueConstraint(
            "creator_id", "plan_id", "client_event_id", name="uq_creator_workspace_pref_idempotency"
        ),
    )
    op.create_index(
        "idx_creator_workspace_pref_plan_created",
        "creator_workspace_preference_signals",
        ["plan_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_creator_workspace_pref_plan_created", table_name="creator_workspace_preference_signals"
    )
    op.drop_table("creator_workspace_preference_signals")
    op.drop_index(
        "idx_creator_workspace_deliverables_item", table_name="creator_workspace_deliverables"
    )
    op.drop_table("creator_workspace_deliverables")
    op.drop_index(
        "idx_creator_workspace_receipts_plan_created", table_name="creator_workspace_receipts"
    )
    op.drop_table("creator_workspace_receipts")
