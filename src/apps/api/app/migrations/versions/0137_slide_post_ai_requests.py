"""Durable SlidePost AI results and permanent paid execution claims."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "slide_post_ai_requests",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("project_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("input_digest", sa.Text(), nullable=False),
        sa.Column("request_input", pg.JSONB(), nullable=False),
        sa.Column("resolved_input", pg.JSONB()),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("claim_id", pg.UUID(as_uuid=True)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("result", pg.JSONB()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('propose', 'chat_edit')", name="ck_slide_ai_kind"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'unknown', 'rejected')",
            name="ck_slide_ai_status",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'rejected') OR claim_id IS NOT NULL", name="ck_slide_ai_claim"
        ),
        sa.CheckConstraint(
            "status != 'completed' OR result IS NOT NULL", name="ck_slide_ai_result"
        ),
    )
    op.create_index("ix_slide_ai_owner_item", "slide_post_ai_requests", ["owner_id", "item_id"])


def downgrade():
    op.drop_table("slide_post_ai_requests")
