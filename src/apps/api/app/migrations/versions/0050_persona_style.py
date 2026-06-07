"""Add personas.style JSONB for per-user persistent text style (M1).

Revision ID: 0050
Revises: 0049
Create Date: 2026-06-07

Stores the per-user derived style: a pinned style-set id + parity-safe knob
overrides (font, size, position, colors, anchor) + footage/instruction preferences.
Nullable — users without a derived style keep current byte-identical render behavior.
Derived by nova.plan.style_derivation; user-editable via PATCH /personas/style.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "personas",
        sa.Column(
            "style",
            postgresql.JSONB(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("personas", "style")
