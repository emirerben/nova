"""Persist explicit Main Creator automatic-iteration consent and cap."""

import sqlalchemy as sa
from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "creator_agent_sessions",
        sa.Column("auto_iteration_opt_in", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "creator_agent_sessions",
        sa.Column("automatic_revision_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "ck_creator_agent_sessions_auto_revision_count",
        "creator_agent_sessions",
        "automatic_revision_count >= 0 AND automatic_revision_count <= 1",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_creator_agent_sessions_auto_revision_count",
        "creator_agent_sessions",
        type_="check",
    )
    op.drop_column("creator_agent_sessions", "automatic_revision_count")
    op.drop_column("creator_agent_sessions", "auto_iteration_opt_in")
