"""Add audio_gcs_path to video_templates.

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-23

Zero-downtime safe — additive nullable column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("video_templates", sa.Column("audio_gcs_path", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("video_templates", "audio_gcs_path")
