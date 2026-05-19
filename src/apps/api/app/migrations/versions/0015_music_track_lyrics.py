"""Add lyric extraction columns to music_tracks.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-19

Lyric extraction stores Genius lyric text plus Whisper word-level timings
aligned into a structured per-line/per-word JSON cache. Status is tracked
independently from beat-detection so that lyric extraction failures (no
Genius hit, Whisper API down) do not block the track from appearing in the
gallery.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "music_tracks",
        sa.Column(
            "lyrics_status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "music_tracks",
        sa.Column("lyrics_cached", postgresql.JSONB, nullable=True),
    )
    op.add_column(
        "music_tracks",
        sa.Column("lyrics_error_detail", sa.Text(), nullable=True),
    )
    op.add_column(
        "music_tracks",
        sa.Column(
            "lyrics_extracted_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "music_tracks",
        sa.Column("lyrics_source", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_music_tracks_lyrics_status",
        "music_tracks",
        ["lyrics_status"],
    )


def downgrade() -> None:
    op.drop_index("idx_music_tracks_lyrics_status", table_name="music_tracks")
    op.drop_column("music_tracks", "lyrics_source")
    op.drop_column("music_tracks", "lyrics_extracted_at")
    op.drop_column("music_tracks", "lyrics_error_detail")
    op.drop_column("music_tracks", "lyrics_cached")
    op.drop_column("music_tracks", "lyrics_status")
