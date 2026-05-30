"""Add plan_items.rationale (surface the AI's reasoning in the dashboard).

Revision ID: 0041
Revises: 0040
Create Date: 2026-05-30

The content_plan_generator now emits a short per-item `rationale` (the AI's "why
this video works + which proven lever it pulls"), shown read-only in the plan
dashboard. PlanItem stores theme/idea/filming_suggestion as discrete columns, so
rationale gets its own nullable Text column. Nullable with no default — existing
items simply have no rationale and the UI hides the line. The persona's rationale
needs no migration (it lives in the `personas.persona` JSONB).
"""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plan_items", sa.Column("rationale", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("plan_items", "rationale")
