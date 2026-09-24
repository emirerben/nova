"""Append-only Creative Brief requirement ledger per creation thread (KRI-188).

Revision ID: 0111
Revises: 0110
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "creative_brief_versions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "thread_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("creation_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("requirements", pg.JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "source_turn_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("creator_agent_turns.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="ck_creative_brief_versions_version"),
        sa.UniqueConstraint(
            "thread_id", "version", name="uq_creative_brief_versions_thread_version"
        ),
    )
    op.create_index(
        "uq_creative_brief_versions_turn",
        "creative_brief_versions",
        ["thread_id", "source_turn_id"],
        unique=True,
        postgresql_where=sa.text("source_turn_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_creative_brief_versions_turn", table_name="creative_brief_versions")
    op.drop_table("creative_brief_versions")
