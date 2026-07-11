"""Add voiceover_bed_level to plan_items for the narrated original-audio bed.

Revision ID: 0060
Revises: 0059
Create Date: 2026-06-23

Changes:
  plan_items.voiceover_bed_level: new Float nullable — original clip-audio bed
    level for narrated-walkthrough items (0.0 = voice only, 1.0 = loudest).
    Set via PATCH /plan-items/{id}/voiceover-bed-level; threaded to
    build_generative_job so the footage audio plays, side-chain ducked, under
    the narration. NULL = use Kria's default level.

No backfill needed (existing rows stay NULL = Kria default at render time).
"""

import sqlalchemy as sa
from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column("voiceover_bed_level", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_items", "voiceover_bed_level")
