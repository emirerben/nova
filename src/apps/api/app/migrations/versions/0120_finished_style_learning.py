"""Consented normalized finished-edit evidence and memory provenance (KRI-336).

Revision ID: 0120
Revises: 0119
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column(
            "creator_edit_learning_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "creator_edit_learning_epoch", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column("creator_memory_items", sa.Column("evidence", postgresql.JSONB(), nullable=True))
    op.drop_constraint("ck_creator_memory_source_kind", "creator_memory_items", type_="check")
    op.create_check_constraint(
        "ck_creator_memory_source_kind",
        "creator_memory_items",
        "source_kind IN ('profile','creation_thread','finished_edits')",
    )
    op.create_index(
        "idx_creator_memory_finished_signature",
        "creator_memory_items",
        ["user_id", sa.text("(evidence ->> 'signature')")],
        postgresql_where=sa.text("source_kind = 'finished_edits'"),
    )
    op.create_table(
        "creator_style_learning_projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="SET NULL")
        ),
        sa.Column("evidence_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("consent_epoch", sa.BigInteger(), nullable=False),
        sa.Column("variant_id", sa.Text(), nullable=False),
        sa.Column("pending_recipe_digest", sa.Text(), nullable=False),
        sa.Column("pending_changes", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("finished_changes", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("published_recipe_digest", sa.Text()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("user_id", "plan_item_id", name="uq_style_learning_owner_project"),
        sa.CheckConstraint(
            "evidence_version = 1 AND consent_epoch >= 0", name="ck_style_learning_version"
        ),
    )
    op.create_index(
        "idx_style_learning_owner_finished",
        "creator_style_learning_projects",
        ["user_id", sa.text("finished_at DESC")],
    )
    op.create_index(
        "idx_style_learning_project", "creator_style_learning_projects", ["plan_item_id"]
    )
    op.create_index("idx_style_learning_job", "creator_style_learning_projects", ["job_id"])


def downgrade():
    op.drop_table("creator_style_learning_projects")
    op.execute("DELETE FROM creator_memory_items WHERE source_kind = 'finished_edits'")
    op.drop_constraint("ck_creator_memory_source_kind", "creator_memory_items", type_="check")
    op.create_check_constraint(
        "ck_creator_memory_source_kind",
        "creator_memory_items",
        "source_kind IN ('profile','creation_thread')",
    )
    op.drop_index("idx_creator_memory_finished_signature", table_name="creator_memory_items")
    op.drop_column("creator_memory_items", "evidence")
    op.drop_column("users", "creator_edit_learning_epoch")
    op.drop_column("users", "creator_edit_learning_enabled")
