"""Create plan_item_assets — the per-item visual asset pool (auto-placement PR0, plan 005).

Revision ID: 0063
Revises: 0062
Create Date: 2026-07-02

New table `plan_item_assets`: one row per screenshot / screen recording a creator
drops into a plan item's pool. Feeds the overlay auto-placement matcher (plan 005).
Objects live under the persistent `users/{user_id}/plan/{plan_item_id}/pool/` GCS
prefix (never a 24h-swept path). `analysis`/`duration_s`/`aspect` stay NULL until
the PR1a analysis agents land; `status` lifecycle: uploaded → analyzing → ready | failed.

Purely additive — no existing tables touched; downgrade drops the table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_item_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "plan_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("gcs_path", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("source_filename", sa.Text(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("aspect", sa.Float(), nullable=True),
        sa.Column("analysis", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="uploaded"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_plan_item_assets_item_created",
        "plan_item_assets",
        ["plan_item_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_plan_item_assets_item_created", table_name="plan_item_assets")
    op.drop_table("plan_item_assets")
