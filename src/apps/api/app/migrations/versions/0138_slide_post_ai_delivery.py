"""Original-input delivery for never-claimed SlidePost AI requests."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("slide_post_ai_requests", sa.Column("prepared_input", pg.JSONB()))
    op.add_column("slide_post_ai_requests", sa.Column("authority_snapshot", pg.JSONB()))
    op.add_column(
        "slide_post_ai_requests", sa.Column("next_delivery_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "slide_post_ai_requests",
        sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_slide_ai_delivery_attempts", "slide_post_ai_requests", "delivery_attempts >= 0"
    )
    op.create_index(
        "ix_slide_ai_delivery", "slide_post_ai_requests", ["status", "next_delivery_at"]
    )


def downgrade():
    op.drop_index("ix_slide_ai_delivery", table_name="slide_post_ai_requests")
    op.drop_constraint("ck_slide_ai_delivery_attempts", "slide_post_ai_requests")
    for name in ("prepared_input", "authority_snapshot", "next_delivery_at", "delivery_attempts"):
        op.drop_column("slide_post_ai_requests", name)
