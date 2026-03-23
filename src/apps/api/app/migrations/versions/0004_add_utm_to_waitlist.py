"""Add UTM tracking columns to waitlist_signups.

Revision ID: 0004
Revises: 0003_template_audio_path
Create Date: 2026-03-23

Adds three nullable Text columns for server-side UTM attribution:
  - utm_source: traffic source (e.g., "tiktok", "google")
  - utm_medium: marketing medium (e.g., "social", "cpc")
  - utm_campaign: campaign name (e.g., "launch-v1")

All nullable — NULL when UTM params are absent from the signup URL.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003_template_audio_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("waitlist_signups", sa.Column("utm_source", sa.Text(), nullable=True))
    op.add_column("waitlist_signups", sa.Column("utm_medium", sa.Text(), nullable=True))
    op.add_column("waitlist_signups", sa.Column("utm_campaign", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("waitlist_signups", "utm_campaign")
    op.drop_column("waitlist_signups", "utm_medium")
    op.drop_column("waitlist_signups", "utm_source")
