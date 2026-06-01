"""Create content_plans table (content-plan Phase 2, data model).

Revision ID: 0037
Revises: 0036
Create Date: 2026-05-29

A parent entity owning N plan_items. FKs to users and personas. Additive,
no behavior change. plan_items (0038) FKs back to this table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_plans",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "persona_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("personas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("events", postgresql.JSONB(), nullable=True),
        sa.Column("plan_status", sa.Text(), nullable=False, server_default="generating"),
        sa.Column("horizon_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
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
    )
    op.create_index("idx_content_plans_user_id", "content_plans", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_content_plans_user_id", table_name="content_plans")
    op.drop_table("content_plans")
