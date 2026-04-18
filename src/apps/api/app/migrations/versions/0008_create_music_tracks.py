"""Create music_tracks table for beat-sync template type.

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "music_tracks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("artist", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("audio_gcs_path", sa.Text(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("beat_timestamps_s", JSONB(), nullable=True),
        sa.Column("analysis_status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("track_config", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_music_tracks_status", "music_tracks", ["analysis_status"])
    op.create_index("idx_music_tracks_published", "music_tracks", ["published_at"])


def downgrade() -> None:
    op.drop_index("idx_music_tracks_published", table_name="music_tracks")
    op.drop_index("idx_music_tracks_status", table_name="music_tracks")
    op.drop_table("music_tracks")
