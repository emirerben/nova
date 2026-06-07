"""Add plan_items.conformance (nullable JSONB) for ConformanceFeedbackAgent results.

Revision ID: 0051
Revises: 0050
Create Date: 2026-06-07

Stores the per-attachment conformance feedback verdict from ConformanceFeedbackAgent:
{verdict, confidence, summary, mismatches[], suggestions[]}. Nullable — only populated
after clip attach when CONFORMANCE_FEEDBACK_ENABLED=True. Legacy rows remain NULL (no
backfill needed — conformance is best-effort and display-only; the Generate button is
never blocked on it).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column(
            "conformance",
            postgresql.JSONB(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("plan_items", "conformance")
