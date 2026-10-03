"""Store capture date/place on pool assets so slide photos carry filming context.

Revision ID: 0114
Revises: 0113
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_item_assets",
        sa.Column("capture", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_item_assets", "capture")
