"""Add content_plans.pool (JSONB) — the post-activation footage pool.

Revision ID: 0053
Revises: 0052
Create Date: 2026-06-11

pool shape: {"status": "matching"|"matched"|"matched_empty"|"match_failed",
             "clips": [{"gcs_path": str, "matched_item_id": str | null}],
             "updated_at": iso8601}
NULL = no pool uploaded yet. Clips land under users/{uid}/plan-pool/{plan_id}/
(persistent prefix, not swept by the 24h GCS rule). match_pool_clips distributes
them across pending plan items as machine_matched provisional assignments.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "content_plans",
        sa.Column("pool", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("content_plans", "pool")
