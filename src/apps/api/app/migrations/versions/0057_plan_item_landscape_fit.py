"""Add landscape_fit to plan_items for the per-item landscape-clip preference.

Revision ID: 0057
Revises: 0056
Create Date: 2026-06-26

Changes:
  plan_items.landscape_fit: new Text NOT NULL server_default='fit'.
    "fit"  = letterbox the clip (full-width, solid black bars, never enlarged)
    "fill" = center-crop to fill the 9:16 frame (previous hard-coded behavior)
  Only affects landscape source clips (width > height); portrait / square clips
  always crop regardless of this setting.

  server_default='fit' backfills every existing row to "fit" so existing plan
  items immediately keep landscape clips horizontal — no separate backfill needed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column("landscape_fit", sa.Text(), nullable=False, server_default="fit"),
    )


def downgrade() -> None:
    op.drop_column("plan_items", "landscape_fit")
