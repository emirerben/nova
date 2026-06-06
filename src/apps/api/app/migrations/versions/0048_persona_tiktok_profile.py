"""Add personas.tiktok_profile JSONB for onboarding TikTok import.

Revision ID: 0048
Revises: 0047
Create Date: 2026-06-06

Stores the scraped public TikTok profile (handle, follower_count, video_count,
top_captions, top_hashtags, analyzed_at) from the pre-screen onboarding step.
Nullable — users who skip the TikTok pre-screen or whose scrape fails stay NULL.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "personas",
        sa.Column(
            "tiktok_profile",
            postgresql.JSONB(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("personas", "tiktok_profile")
