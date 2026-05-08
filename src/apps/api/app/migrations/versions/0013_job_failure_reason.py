"""Add jobs.failure_reason column for structured failure taxonomy.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-08

Zero-downtime safe:
- Additive nullable column. Existing rows stay NULL until a new failure
  classifies them; happy-path jobs never write to it.
- Indexed because admin dashboards filter on it ("show me all jobs that
  failed with `template_assets_missing`").
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("failure_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_jobs_failure_reason",
        "jobs",
        ["failure_reason"],
    )


def downgrade() -> None:
    op.drop_index("idx_jobs_failure_reason", table_name="jobs")
    op.drop_column("jobs", "failure_reason")
