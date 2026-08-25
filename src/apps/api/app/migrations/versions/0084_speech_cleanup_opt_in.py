"""Add opt-in Speech cleanup consent state to plan items."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column(
            "speech_cleanup_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "plan_items",
        sa.Column("speech_cleanup_notice", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_items", "speech_cleanup_notice")
    op.drop_column("plan_items", "speech_cleanup_enabled")
