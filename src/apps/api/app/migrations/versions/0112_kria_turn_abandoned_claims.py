"""Count abandoned runtime-v2 turn claims so a stuck turn stops re-planning.

Revision ID: 0112
Revises: 0111
"""

import sqlalchemy as sa
from alembic import op

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "creator_agent_turns",
        sa.Column("abandoned_claims", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_creator_agent_turns_abandoned_claims",
        "creator_agent_turns",
        "abandoned_claims >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_creator_agent_turns_abandoned_claims", "creator_agent_turns", type_="check"
    )
    op.drop_column("creator_agent_turns", "abandoned_claims")
