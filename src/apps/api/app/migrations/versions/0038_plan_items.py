"""Create plan_items table + current_job_id FK (content-plan Phase 2).

Revision ID: 0038
Revises: 0037
Create Date: 2026-05-29

plan_items.current_job_id -> jobs.id is the first half of the circular FK pair.
It lands HERE (jobs already exists). The second half, jobs.content_plan_item_id
-> plan_items.id, lands in 0039 (after plan_items exists). Both columns are
nullable so neither migration can deadlock. See plan, Data model section.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "content_plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("day_index", sa.Integer(), nullable=False),
        sa.Column("theme", sa.Text(), nullable=False),
        sa.Column("idea", sa.Text(), nullable=False),
        sa.Column("filming_suggestion", sa.Text(), nullable=True),
        sa.Column("clip_gcs_paths", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("item_status", sa.Text(), nullable=False, server_default="idea"),
        sa.Column(
            "current_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id"),
            nullable=True,
        ),
        sa.Column("user_edited", sa.Boolean(), nullable=False, server_default=sa.text("false")),
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
    op.create_index(
        "idx_plan_items_content_plan_id_day",
        "plan_items",
        ["content_plan_id", "day_index"],
    )


def downgrade() -> None:
    op.drop_index("idx_plan_items_content_plan_id_day", table_name="plan_items")
    op.drop_table("plan_items")
