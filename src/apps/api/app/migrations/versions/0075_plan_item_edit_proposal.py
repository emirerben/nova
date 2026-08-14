"""Add the versioned guided-edit proposal envelope.

Revision ID: 0075
Revises: 0074
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column("edit_proposal", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    # Feature rollback is flag/code rollback; approved creator work must remain
    # readable. Refuse a schema downgrade once the column contains any data
    # rather than silently deleting drafts and approvals.
    has_proposals = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM plan_items WHERE edit_proposal IS NOT NULL LIMIT 1"))
        .scalar()
    )
    if has_proposals:
        raise RuntimeError("cannot drop plan_items.edit_proposal while guided-edit proposals exist")
    op.drop_column("plan_items", "edit_proposal")
