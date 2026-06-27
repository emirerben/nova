"""Add sound_effects table for the admin-curated SFX glossary.

Revision ID: 0059
Revises: 0058
Create Date: 2026-06-27

Changes:
  sound_effects: admin-registered short audio clips (click sounds, meme stings, etc.)
  that users can place at arbitrary timestamps in their generated videos.
  status: "pending" | "ready" | "failed"  (no analysis stage unlike MusicTrack)
  published_at: non-null = visible in the public glossary picker.
  archived_at: non-null = soft-archived (hidden; rows kept so existing placements keep
  their audio_gcs_path reference).
"""

import sqlalchemy as sa
from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sound_effects",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("audio_gcs_path", sa.Text(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("source_filename", sa.Text(), nullable=True),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_sound_effects_status", "sound_effects", ["status"])
    op.create_index("idx_sound_effects_published", "sound_effects", ["published_at"])
    op.create_index("idx_sound_effects_created_at", "sound_effects", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_sound_effects_created_at", table_name="sound_effects")
    op.drop_index("idx_sound_effects_published", table_name="sound_effects")
    op.drop_index("idx_sound_effects_status", table_name="sound_effects")
    op.drop_table("sound_effects")
