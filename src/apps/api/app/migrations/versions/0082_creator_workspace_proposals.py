"""Persist approval-gated off-plan creator workspace proposals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "creator_workspace_proposals",
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
        sa.Column("media_ids", postgresql.JSONB(), nullable=False),
        sa.Column("media_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("relevance", sa.Text(), nullable=True),
        sa.Column(
            "target_plan_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "result_plan_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("topic", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("proposal_hash", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("decision_client_event_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('pending','ready','failed','approved','rejected')",
            name="ck_creator_workspace_proposals_status",
        ),
        sa.CheckConstraint(
            "relevance IS NULL OR relevance IN ('existing_item','new_topic','unmatched')",
            name="ck_creator_workspace_proposals_relevance",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('accept_existing','accept_new_topic','reject')",
            name="ck_creator_workspace_proposals_decision",
        ),
        sa.CheckConstraint("ownership_epoch >= 0", name="ck_creator_workspace_proposals_epoch"),
        sa.UniqueConstraint(
            "creator_id", "idempotency_key", name="uq_creator_workspace_proposals_idempotency"
        ),
    )
    op.create_index(
        "idx_creator_workspace_proposals_plan_created",
        "creator_workspace_proposals",
        ["plan_id", "created_at"],
    )
    op.create_index(
        "idx_creator_workspace_proposals_creator_status",
        "creator_workspace_proposals",
        ["creator_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_creator_workspace_proposals_creator_status",
        table_name="creator_workspace_proposals",
    )
    op.drop_index(
        "idx_creator_workspace_proposals_plan_created",
        table_name="creator_workspace_proposals",
    )
    op.drop_table("creator_workspace_proposals")
