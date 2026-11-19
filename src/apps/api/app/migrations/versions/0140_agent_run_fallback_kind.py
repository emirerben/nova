"""Record behavioural fallbacks on agent_run (KRI-1429).

Revision ID: 0140
Revises: 0139

A run can end ``ok`` while its parse boundary kept less than the model answered (an editorial
plan dropped, matcher entries refused). ``fallback_kind`` holds the comma-joined kinds the
output reported, NULL for a clean run, so the fleet report can show a rate per agent. A nullable
column with no default: existing rows and rows from older workers stay NULL.
"""

import sqlalchemy as sa
from alembic import op

revision = "0140"
down_revision = "0139"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_run", sa.Column("fallback_kind", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_run", "fallback_kind")
