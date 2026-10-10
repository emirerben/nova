"""Persist provider-marked interactive thought summaries (KRI-557)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "thought_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creation_threads.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "plan_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
        ),
        sa.Column("client_request_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="streaming"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(
            "status IN ('streaming', 'completed', 'failed')", name="ck_thought_summaries_status"
        ),
        sa.CheckConstraint("length(text) <= 12000", name="ck_thought_summaries_text_length"),
        sa.CheckConstraint(
            "(thread_id IS NOT NULL)::int + (plan_item_id IS NOT NULL)::int = 1",
            name="ck_thought_summaries_subject",
        ),
    )
    op.create_index(
        "idx_thought_summaries_thread_request",
        "thought_summaries",
        ["thread_id", "client_request_id", "started_at"],
    )
    op.create_index(
        "idx_thought_summaries_item_request",
        "thought_summaries",
        ["plan_item_id", "client_request_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_table("thought_summaries")
