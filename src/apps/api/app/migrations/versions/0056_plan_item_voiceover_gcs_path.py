"""Add voiceover_gcs_path to plan_items for the narrated-walkthrough archetype.

Revision ID: 0056
Revises: 0055
Create Date: 2026-06-19

Changes:
  plan_items.voiceover_gcs_path: new Text nullable — GCS key of a user-recorded
    or user-uploaded voiceover for narrated-walkthrough items. Set via
    PATCH /plan-items/{id}/voiceover; threaded to build_generative_job as
    voiceover_gcs_path at generate time so the narrated archetype can do
    force-alignment and per-step clip trimming.

No backfill needed (existing rows stay NULL = no narrated voiceover).
"""

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column("voiceover_gcs_path", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_items", "voiceover_gcs_path")
