"""Bound retries of persisted SlidePost result publication without recompute."""

import sqlalchemy as sa
from alembic import op

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "slide_post_generation_receipts",
        sa.Column("finalize_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "slide_post_generation_receipts",
        sa.Column("next_finalize_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_slide_post_receipt_finalize_attempts",
        "slide_post_generation_receipts",
        "finalize_attempts >= 0",
    )


def downgrade():
    op.drop_constraint("ck_slide_post_receipt_finalize_attempts", "slide_post_generation_receipts")
    op.drop_column("slide_post_generation_receipts", "next_finalize_at")
    op.drop_column("slide_post_generation_receipts", "finalize_attempts")
