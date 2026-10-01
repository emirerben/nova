"""Carry the editor's unsaved state on a runtime-v2 turn until it completes.

Revision ID: 0113
Revises: 0112
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "creator_agent_turns",
        sa.Column("editor_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("creator_agent_turns", "editor_state")
