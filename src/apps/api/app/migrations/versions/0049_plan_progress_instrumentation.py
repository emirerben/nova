"""plan progress instrumentation

Revision ID: 0049
Revises: 0048
Create Date: 2026-06-06
"""

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "personas",
        sa.Column("generation_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "content_plans",
        sa.Column("generation_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "content_plans",
        sa.Column("activation_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "content_plans",
        sa.Column("activation_phase", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("content_plans", "activation_phase")
    op.drop_column("content_plans", "activation_started_at")
    op.drop_column("content_plans", "generation_started_at")
    op.drop_column("personas", "generation_started_at")
