"""Add jobs.content_plan_item_id FK (content-plan Phase 2, circular FK 2nd half).

Revision ID: 0039
Revises: 0038
Create Date: 2026-05-29

The second half of the circular FK pair. jobs.content_plan_item_id ->
plan_items.id lands here, after plan_items exists (0038). Nullable: every
non-plan job leaves it NULL. Indexed for the admin job-debug reverse lookup.
No behavior change for existing jobs.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "content_plan_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id"),
            nullable=True,
        ),
    )
    op.create_index("idx_jobs_content_plan_item_id", "jobs", ["content_plan_item_id"])


def downgrade() -> None:
    op.drop_index("idx_jobs_content_plan_item_id", table_name="jobs")
    op.drop_column("jobs", "content_plan_item_id")
