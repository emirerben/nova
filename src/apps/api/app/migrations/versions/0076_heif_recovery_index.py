"""Index the bounded HEIC/HEIF decoder recovery sweep.

Revision ID: 0076
Revises: 0075
Create Date: 2026-08-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_plan_item_assets_heif_unreadable_recovery",
        "plan_item_assets",
        ["id"],
        postgresql_where=sa.text(
            "status = 'failed' AND error_code = 'analysis_unreadable' "
            "AND upload_content_type IN ('image/heic', 'image/heif') "
            "AND analysis_attempt_count < 2"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_plan_item_assets_heif_unreadable_recovery",
        table_name="plan_item_assets",
    )
