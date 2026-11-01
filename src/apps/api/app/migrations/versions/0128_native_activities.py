"""Owner-scoped native completion and attention inbox."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "native_activities",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
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
            "item_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("variant_id", sa.Text(), nullable=False),
        sa.Column("ownership_epoch", sa.BigInteger(), nullable=False),
        sa.Column("identity", pg.JSONB(), nullable=False),
        sa.Column("generation", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("semantic_key", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('video_ready', 'action_required')", name="ck_native_activity_kind"
        ),
        sa.CheckConstraint(
            "ownership_epoch >= 0 AND length(variant_id) > 0 AND length(generation) > 0",
            name="ck_native_activity_target",
        ),
    )
    op.create_index(
        "idx_native_activity_owner_created", "native_activities", ["owner_id", "created_at", "id"]
    )


def downgrade():
    op.drop_table("native_activities")
