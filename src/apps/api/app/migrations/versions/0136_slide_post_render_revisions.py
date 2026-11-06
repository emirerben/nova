"""Durable keyed SlidePost saved-version render intents."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "slide_post_render_revisions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "parent_request_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("slide_post_generation_receipts.client_request_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "item_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ownership_epoch", sa.BigInteger(), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("draft_snapshot", pg.JSONB(), nullable=False),
        sa.Column("asset_snapshot", pg.JSONB(), nullable=False),
        sa.Column("snapshot_digest", sa.Text(), nullable=False),
        sa.Column("native_client", sa.Boolean(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column("publish_attempts", sa.Integer(), nullable=False),
        sa.Column("finalize_attempts", sa.Integer(), nullable=False),
        sa.Column("next_observe_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("staged_result", pg.JSONB(none_as_null=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "parent_request_id", "draft_version", name="uq_slide_post_revision_version"
        ),
        sa.CheckConstraint(
            "state IN ('pending','running','completed','failed','superseded','blocked')",
            name="ck_slide_post_revision_state",
        ),
        sa.CheckConstraint(
            "publish_attempts >= 0 AND finalize_attempts >= 0",
            name="ck_slide_post_revision_attempts",
        ),
    )
    op.create_index(
        "ix_slide_post_revision_observe",
        "slide_post_render_revisions",
        ["state", "next_observe_at"],
    )


def downgrade():
    op.drop_table("slide_post_render_revisions")
