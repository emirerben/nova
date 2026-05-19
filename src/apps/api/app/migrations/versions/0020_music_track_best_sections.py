"""Add best_sections + section_version to music_tracks.

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-15

Phase 2 (library side) of the auto-music feature. Adds two nullable
columns that hold the output of the new ``song_sections`` agent:

  - ``best_sections`` (JSONB): an ordered list of ``SongSection`` blobs
    (1-3 ranked sections; rank 1 is best). Schema:
    ``[{rank, start_s, end_s, label, energy, suggested_use, rationale}]``
  - ``section_version`` (text): mirrors
    ``CURRENT_SECTION_VERSION`` so the matcher can refuse stale rows
    without parsing the JSONB.

Both columns are nullable; existing rows continue to satisfy the schema
with NULLs. The auto-music matcher filters to rows where
``best_sections IS NOT NULL AND section_version = CURRENT_SECTION_VERSION``,
so unsectioned (or stale-sectioned) tracks are invisible to the new flow
while the manual music-pick flow keeps working unchanged.

Downgrade drops both columns. No data preservation needed — sections are
idempotently regenerable by re-running the song_sections backfill.
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "music_tracks",
        sa.Column("best_sections", sa.dialects.postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "music_tracks",
        sa.Column("section_version", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("music_tracks", "section_version")
    op.drop_column("music_tracks", "best_sections")
