"""Add ai_labels + label_version to music_tracks.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-15

Phase 0 of the auto-music feature (see plans/our-current-agentic-template-
scalable-gem.md). Adds two nullable columns that hold the creative labels
produced by the new ``song_classifier`` agent:

  - ``ai_labels`` (JSONB): a ``MusicLabels`` blob — genre, vibe_tags,
    energy, pacing, mood, ideal_content_profile, copy_tone,
    transition_style, color_grade.
  - ``label_version`` (text): mirrors ``MusicLabels.label_version`` so
    the matcher can refuse stale rows without parsing the JSONB.

Both columns are nullable; existing rows continue to satisfy the schema
with NULLs. The auto-music matcher filters to rows where
``ai_labels IS NOT NULL AND label_version = CURRENT_LABEL_VERSION``, so
unlabeled (or stale-labeled) tracks are invisible to the new flow while
the manual music-pick flow keeps working unchanged.

Downgrade drops both columns. No data preservation needed — labels are
idempotently regenerable by re-running the song_classifier backfill.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "music_tracks",
        sa.Column("ai_labels", sa.dialects.postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "music_tracks",
        sa.Column("label_version", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("music_tracks", "label_version")
    op.drop_column("music_tracks", "ai_labels")
