"""Add music_track_id FK to jobs table.

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-17

music_track_id and template_id are mutually exclusive:
- template jobs set template_id, leave music_track_id NULL
- music jobs set music_track_id, leave template_id NULL
- default jobs leave both NULL
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "music_track_id",
            sa.Text(),
            sa.ForeignKey("music_tracks.id"),
            nullable=True,
        ),
    )
    op.create_index("idx_jobs_music_track_id", "jobs", ["music_track_id"])


def downgrade() -> None:
    op.drop_index("idx_jobs_music_track_id", table_name="jobs")
    op.drop_column("jobs", "music_track_id")
