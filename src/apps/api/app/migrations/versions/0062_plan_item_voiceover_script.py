"""Add voiceover_script + voiceover_script_recorded_version to plan_items for the
"Get a transcript" helper.

Revision ID: 0062
Revises: 0061
Create Date: 2026-07-01

Changes:
  plan_items.voiceover_script: new JSONB nullable — the AI-authored voiceover
    script the creator reads while recording (validated by
    app.schemas.voiceover_script.VoiceoverScript). NULL until the creator
    generates a transcript; `version` inside the blob bumps on each Rewrite.
  plan_items.voiceover_script_recorded_version: new Integer nullable — the
    script `version` the currently-attached voiceover take was recorded against,
    so the Script step can warn when a later Rewrite invalidates the take.
    NULL until a take is captured through the transcript flow.

Both additive + nullable; existing rows stay NULL (feature-off behavior). No backfill.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column("voiceover_script", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "plan_items",
        sa.Column("voiceover_script_recorded_version", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_items", "voiceover_script_recorded_version")
    op.drop_column("plan_items", "voiceover_script")
