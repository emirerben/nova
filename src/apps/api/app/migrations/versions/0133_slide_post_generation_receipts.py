"""Durable SlidePost generation identity and immutable first-render input."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "slide_post_generation_receipts",
        sa.Column("client_request_id", pg.UUID(as_uuid=True), nullable=False),
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
        sa.Column("ownership_epoch", sa.BigInteger(), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("job_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_snapshot", pg.JSONB(), nullable=False),
        sa.Column("asset_snapshot", pg.JSONB(), nullable=False),
        sa.Column("snapshot_digest", sa.Text(), nullable=False),
        sa.Column("dispatch_state", sa.Text(), nullable=False, server_default="unconfirmed"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("client_request_id", name="pk_slide_post_generation_receipts"),
        sa.UniqueConstraint("job_id", name="uq_slide_post_generation_receipts_job_id"),
        sa.CheckConstraint(
            "draft_version >= 1 AND ownership_epoch >= 0", name="ck_slide_post_receipt_version"
        ),
        sa.CheckConstraint(
            "dispatch_state IN ('unconfirmed','published','publish_failed')",
            name="ck_slide_post_receipt_dispatch",
        ),
    )


def downgrade():
    op.drop_table("slide_post_generation_receipts")
