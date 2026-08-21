"""Add the server-authoritative plan-item audio mode.

Revision ID: 0078
Revises: 0077
Create Date: 2026-08-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column("audio_mode", sa.Text(), nullable=False, server_default="kria"),
    )
    # Rows created before audio_mode existed used voiceover_gcs_path as the sole
    # soundtrack intent. Preserve that intent instead of silently switching
    # existing narrated items back to Kria-decides during the rollout.
    op.execute(
        sa.text(
            """
            UPDATE plan_items
            SET audio_mode = 'voiceover'
            WHERE voiceover_gcs_path IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_column("plan_items", "audio_mode")
