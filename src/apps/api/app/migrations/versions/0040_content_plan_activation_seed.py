"""Add content_plans activation-seed columns (content-plan T8).

Revision ID: 0040
Revises: 0039
Create Date: 2026-05-29

The activation seed lets a user upload one batch of recent clips after their plan
is ready; `clip_plan_matcher` assigns the best-fit clips to plan items and auto-
generates the top picks. Two new columns on `content_plans`:

  - `seed_clip_paths` JSONB — the uploaded seed batch (GCS paths under the
    persistent `users/{uid}/plan/{plan_id}/seed/` prefix).
  - `activation_status` Text — the status-poll scalar:
    none -> seeding -> activating -> activated | activated_empty | failed.

Both carry server defaults, so the column adds are nullable-safe with no backfill
and no behavior change for existing plans. Per-item render state stays derived
from Job.status (plan T2) — there is no per-item activation flag.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "content_plans",
        sa.Column(
            "seed_clip_paths",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "content_plans",
        sa.Column(
            "activation_status",
            sa.Text(),
            nullable=False,
            server_default="none",
        ),
    )


def downgrade() -> None:
    op.drop_column("content_plans", "activation_status")
    op.drop_column("content_plans", "seed_clip_paths")
