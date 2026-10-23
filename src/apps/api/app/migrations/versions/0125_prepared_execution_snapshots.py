"""Prepared execution snapshots pinned to an approval (KRI-722).

Revision ID: 0125
Revises: 0124
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "creator_prepared_executions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "creator_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "thread_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("creation_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "approval_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("creator_agent_approvals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "draft_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("creator_edit_drafts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("snapshot", pg.JSONB(), nullable=False),
        sa.Column("snapshot_digest", sa.Text(), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("approval_id", name="uq_creator_prepared_executions_approval"),
        sa.CheckConstraint(
            "octet_length(snapshot::text) <= 262144",
            name="ck_creator_prepared_executions_size",
        ),
    )
    op.create_index(
        "idx_creator_prepared_executions_draft", "creator_prepared_executions", ["draft_id"]
    )
    op.add_column(
        "creator_agent_approvals",
        sa.Column("prepared_execution_id", pg.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_creator_agent_approvals_prepared_execution",
        "creator_agent_approvals",
        "creator_prepared_executions",
        ["prepared_execution_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint(
        "fk_creator_agent_approvals_prepared_execution",
        "creator_agent_approvals",
        type_="foreignkey",
    )
    op.drop_column("creator_agent_approvals", "prepared_execution_id")
    op.drop_index("idx_creator_prepared_executions_draft", "creator_prepared_executions")
    op.drop_table("creator_prepared_executions")
