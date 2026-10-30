"""Durable generated media admission receipts.

Revision ID: 0127
Revises: 0126
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "generated_media_operations",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_item_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_event_id", sa.Text(), nullable=False),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column(
            "request_metadata", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("capability", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("pending_content", pg.BYTEA(), nullable=True),
        *[
            sa.Column(name, sa.Text(), nullable=True)
            for name in (
                "mime_type",
                "content_sha256",
                "model",
                "provider_request_id",
                "editable_title",
                "output_path",
                "output_generation",
                "failure_code",
            )
        ],
        sa.Column("usage", pg.JSONB(), nullable=True),
        sa.Column("reservation_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "asset_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("plan_item_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now() + interval '24 hours'"),
        ),
        sa.UniqueConstraint(
            "user_id", "plan_item_id", "client_event_id", name="uq_generated_media_event"
        ),
        sa.CheckConstraint(
            "capability IN ('image', 'image_edit', 'narration')",
            name="ck_generated_media_capability",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'submitting', 'provider_complete', 'ready', 'unknown', 'failed')",
            name="ck_generated_media_state",
        ),
        sa.CheckConstraint(
            "pending_content IS NULL OR octet_length(pending_content) BETWEEN 1 AND 20971520",
            name="ck_generated_media_bytes",
        ),
    )
    op.create_index("idx_generated_media_expiry", "generated_media_operations", ["expires_at"])


def downgrade():
    op.drop_index("idx_generated_media_expiry", "generated_media_operations")
    op.drop_table("generated_media_operations")
