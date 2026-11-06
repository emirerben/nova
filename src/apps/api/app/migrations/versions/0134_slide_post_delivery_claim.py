"""Permanent keyed SlidePost claim and bounded pending publication recovery."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column("execution_state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_publish_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("staged_result", pg.JSONB(), nullable=True),
        sa.Column("dispatch_queue", sa.Text(), nullable=False, server_default="plan-jobs"),
    ):
        op.add_column("slide_post_generation_receipts", column)
    op.create_check_constraint(
        "ck_slide_post_receipt_execution",
        "slide_post_generation_receipts",
        "execution_state IN ('pending','running','completed','failed')",
    )
    op.create_check_constraint(
        "ck_slide_post_receipt_attempts", "slide_post_generation_receipts", "publish_attempts >= 0"
    )


def downgrade():
    op.drop_constraint("ck_slide_post_receipt_attempts", "slide_post_generation_receipts")
    op.drop_constraint("ck_slide_post_receipt_execution", "slide_post_generation_receipts")
    for column in (
        "dispatch_queue",
        "staged_result",
        "claimed_at",
        "next_publish_at",
        "publish_attempts",
        "execution_state",
    ):
        op.drop_column("slide_post_generation_receipts", column)
