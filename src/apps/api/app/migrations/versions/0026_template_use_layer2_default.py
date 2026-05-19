"""Add per-template use_layer2_default column.

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-18

NULL = fall through to the global text_overlay_v2_enabled flag (existing behaviour).
True  = always use Layer-2 on reanalysis for this template, regardless of the global flag.
False = always use Layer-1 on reanalysis for this template, regardless of the global flag.

Resolution priority when reanalyze-agentic fires:
  1. ?use_layer2 query param (if present) wins absolutely.
  2. template.use_layer2_default (if not NULL) wins.
  3. settings.text_overlay_v2_enabled (global flag) is the fallback.

No default is set — existing rows are NULL, which preserves their prior behaviour.
"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "video_templates",
        sa.Column("use_layer2_default", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("video_templates", "use_layer2_default")
