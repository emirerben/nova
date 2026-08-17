"""Add preview_gcs_path to plan_item_assets for browser-safe previews.

Revision ID: 0077
Revises: 0076
Create Date: 2026-08-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_item_assets",
        sa.Column("preview_gcs_path", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_item_assets", "preview_gcs_path")
