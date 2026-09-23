"""Admin-authored phone subtitled lane request on plan items (KRI-174 Phase 1.5).

Revision ID: 0110
Revises: 0109
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column("phone_lane_request", pg.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_items", "phone_lane_request")
