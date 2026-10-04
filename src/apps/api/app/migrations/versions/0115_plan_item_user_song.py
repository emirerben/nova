"""Add creator-uploaded song columns to plan_items (KRI-374).

A creator can attach their own song to a montage. The song lives in its own
columns, deliberately separate from the voiceover columns, so narration /
voiceover routing never fires for it. ``audio_mode`` is free text (no check
constraint), so the new ``"song"`` value needs no DDL.

Revision ID: 0115
Revises: 0114
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plan_items", sa.Column("song_gcs_path", sa.Text(), nullable=True))
    op.add_column("plan_items", sa.Column("song_generation", sa.BigInteger(), nullable=True))
    op.add_column("plan_items", sa.Column("song_duration_s", sa.Float(), nullable=True))
    op.add_column("plan_items", sa.Column("song_filename", sa.Text(), nullable=True))
    op.add_column(
        "plan_items",
        sa.Column("song_analysis", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "plan_items",
        sa.Column("song_alignment", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_items", "song_alignment")
    op.drop_column("plan_items", "song_analysis")
    op.drop_column("plan_items", "song_filename")
    op.drop_column("plan_items", "song_duration_s")
    op.drop_column("plan_items", "song_generation")
    op.drop_column("plan_items", "song_gcs_path")
