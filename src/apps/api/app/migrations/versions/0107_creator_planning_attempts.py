"""Durable creator footage preparation (KRI-151).

Revision ID: 0107
Revises: 0106
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("creator_agent_sessions", sa.Column("preparation", pg.JSONB(), nullable=True))
    op.create_table(
        "creator_planning_attempts",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("creator_agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_event_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("creator_agent_events.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("creator_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "plan_item_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ownership_epoch", sa.BigInteger(), nullable=False),
        sa.Column("session_revision", sa.Integer(), nullable=False),
        sa.Column("source_digest", sa.Text(), nullable=False),
        sa.Column("inputs", pg.JSONB(), nullable=False),
        sa.Column("planning_action", pg.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("lease_token", sa.Text()),
        sa.Column("lease_until", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_dispatched_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.Text()),
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
            "status IN ('queued','running','completed','failed','superseded')",
            name="ck_creator_planning_attempt_status",
        ),
    )
    op.create_index(
        "idx_creator_planning_attempt_recovery",
        "creator_planning_attempts",
        ["status", "lease_until", "last_dispatched_at"],
    )


def downgrade() -> None:
    op.drop_table("creator_planning_attempts")
    op.drop_column("creator_agent_sessions", "preparation")
