"""Add section_error_detail to music_tracks.

Revision ID: 0034
Revises: 0033
Create Date: 2026-05-28

The song_sections agent is best-effort: _run_song_sections in
app/tasks/music_orchestrate.py catches every non-Refusal Exception and
returns None, leaving best_sections + section_version NULL. Until now
the reason was invisible to admin (only worker logs carried it), so a
track would silently degrade to the legacy 45s auto_best_section
window with no observable cause.

This column persists the last failure reason per track. Successful
runs clear it; silent-fail runs populate it (truncated). See
app/tasks/music_orchestrate.py and app/routes/admin_music.py.
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "music_tracks",
        sa.Column("section_error_detail", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("music_tracks", "section_error_detail")
