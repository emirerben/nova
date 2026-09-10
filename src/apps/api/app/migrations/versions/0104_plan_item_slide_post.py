"""Add the versioned slide-post (mixed-media carousel) draft envelope.

Revision ID: 0104
Revises: 0103
Create Date: 2026-09-09
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column("slide_post", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    # Same rationale as 0075 (edit_proposal): a schema downgrade is a
    # rollback of code/flags, not of creator work. Refuse to drop the column
    # while any draft or approved slide post exists rather than silently
    # deleting it.
    has_slide_posts = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM plan_items WHERE slide_post IS NOT NULL LIMIT 1"))
        .scalar()
    )
    if has_slide_posts:
        raise RuntimeError("cannot drop plan_items.slide_post while slide posts exist")
    op.drop_column("plan_items", "slide_post")
