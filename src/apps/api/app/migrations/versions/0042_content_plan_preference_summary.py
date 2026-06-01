"""Add content_plans.preference_summary (feedback loop, Phase 2).

Revision ID: 0042
Revises: 0041
Create Date: 2026-05-30

The feedback loop compresses a user's video_feedback into a bounded, deterministic
`preference_summary` (signal counts + recent notes) that re-tunes content-plan
generation on a user-triggered regenerate. It is additive AI context cached on the
plan — never a mutation of the items. Nullable with no default: existing plans have
no summary and the generator treats NULL as "(none)".
"""

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("content_plans", sa.Column("preference_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("content_plans", "preference_summary")
