"""Declared deadline for one pool-asset analysis attempt (KRI-820).

Revision ID: 0125
Revises: 0124
"""

import sqlalchemy as sa
from alembic import op

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "plan_item_assets",
        sa.Column("analysis_deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_plan_item_assets_in_flight_deadline",
        "plan_item_assets",
        ["analysis_deadline_at"],
        postgresql_where=sa.text("status IN ('queued', 'analyzing')"),
    )


def downgrade():
    op.drop_index("idx_plan_item_assets_in_flight_deadline", table_name="plan_item_assets")
    op.drop_column("plan_item_assets", "analysis_deadline_at")
