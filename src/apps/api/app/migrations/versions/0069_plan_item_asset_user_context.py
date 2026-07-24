"""Add creator context to plan item visual assets.

Revision ID: 0069
Revises: 0068
Create Date: 2026-07-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plan_item_assets", sa.Column("user_context", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("plan_item_assets", "user_context")
