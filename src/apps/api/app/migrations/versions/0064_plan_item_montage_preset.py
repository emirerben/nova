"""Add montage_preset to plan_items.

Revision ID: 0064
Revises: 0063
Create Date: 2026-07-11

Stores the user's per-item montage visual preset. "classic" preserves the
existing sequential montage render exactly; "masonry" opts into the collage-wall
assembler. Plain Text, no DB CHECK, matching edit_format's extensible pattern.
"""

import sqlalchemy as sa
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column("montage_preset", sa.Text(), nullable=False, server_default="classic"),
    )


def downgrade() -> None:
    op.drop_column("plan_items", "montage_preset")
