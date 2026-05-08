"""Add recipe_cached to music_tracks and make gcs_path nullable + audio_only type.

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-18

Adds recipe storage to music tracks so Gemini audio analysis results
can be cached. Makes VideoTemplate.gcs_path nullable for audio-only
templates. Extends template_type enum with 'audio_only'.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add recipe storage to music tracks
    op.add_column(
        "music_tracks",
        sa.Column("recipe_cached", sa.dialects.postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "music_tracks",
        sa.Column(
            "recipe_cached_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )

    # Allow audio-only templates (no video file)
    op.alter_column("video_templates", "gcs_path", nullable=True)

    # Extend template_type enum to include 'audio_only'
    op.drop_constraint("ck_template_type", "video_templates")
    op.create_check_constraint(
        "ck_template_type",
        "video_templates",
        "template_type IN ('standard', 'music_parent', 'music_child', 'audio_only')",
    )


def downgrade() -> None:
    # Restore template_type constraint without 'audio_only'
    op.drop_constraint("ck_template_type", "video_templates")
    op.create_check_constraint(
        "ck_template_type",
        "video_templates",
        "template_type IN ('standard', 'music_parent', 'music_child')",
    )

    # Restore gcs_path NOT NULL (may fail if audio_only templates exist)
    op.alter_column("video_templates", "gcs_path", nullable=False)

    # Remove recipe columns from music_tracks
    op.drop_column("music_tracks", "recipe_cached_at")
    op.drop_column("music_tracks", "recipe_cached")
