"""Per-run record of a continued Kria turn: why it ran again and what each run spent.

Additive and nullable: code that does not know the column ignores it, and a turn that was
never continued keeps NULL.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("creator_agent_turns", sa.Column("planning_runs", pg.JSONB(), nullable=True))


def downgrade():
    op.drop_column("creator_agent_turns", "planning_runs")
