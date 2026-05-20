"""Add video_templates.lyrics_config for per-template lyrics override.

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-20

NULL on existing AND newly-created rows means "inherit from the linked
MusicTrack.track_config.lyrics_config" — admin edits to the track flow
through automatically until an admin customizes the template's own panel.
Non-NULL (including the empty dict `{}`) means the template's own setting
wins. Resolution happens in template_orchestrate via `is not None`, so the
empty dict is a valid "lyrics explicitly off" state.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "video_templates",
        sa.Column(
            "lyrics_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("video_templates", "lyrics_config")
