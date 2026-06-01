"""Add plan_items.edit_format (format-aware edit engine, Lane A).

Revision ID: 0044
Revises: 0043
Create Date: 2026-05-31

The content_plan agent now declares a per-day edit_format (montage|talking_head|
day_vlog|single_hero) that drives the render archetype dispatch. Stored as plain
Text with a server_default of 'montage' (mirrors item_status) — no DB CHECK, so
the vocabulary can grow without a migration; validation lives in the schema layer
(app.agents._schemas.edit_format). NOT NULL + server_default backfills every
existing row to 'montage', i.e. today's beat-synced behavior — backward-compatible.
"""

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column("edit_format", sa.Text(), nullable=False, server_default="montage"),
    )


def downgrade() -> None:
    op.drop_column("plan_items", "edit_format")
