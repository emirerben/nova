"""Add error_detail column to video_templates.

Revision ID: 0006
Revises: 0005
Create Date: 2026-03-29

Nullable text column for storing analysis failure details (timeout, refusal, etc.).
Backward compatible: nullable add only.
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "video_templates",
        sa.Column("error_detail", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("video_templates", "error_detail")
