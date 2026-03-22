"""add waitlist_signups table

Revision ID: 0001
Revises:
Create Date: 2026-03-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "waitlist_signups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("email", name="uq_waitlist_signups_email"),
    )


def downgrade() -> None:
    op.drop_table("waitlist_signups")
