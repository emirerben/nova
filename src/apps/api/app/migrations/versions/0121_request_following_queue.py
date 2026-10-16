"""Scoped request-following capture cursor and reviewed source versions (KRI-215)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "request_following_collector",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("after_thread_id", pg.UUID(as_uuid=True)),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("summary", pg.JSONB(), nullable=False, server_default="{}"),
        sa.CheckConstraint("id = 1", name="ck_request_following_singleton"),
    )
    op.create_table(
        "request_following_cases",
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
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("eligibility_key", sa.Text(), nullable=False),
        sa.Column("split", sa.Text(), nullable=False),
        sa.Column("capture", pg.JSONB()),
        sa.Column("diagnostics", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("review", pg.JSONB()),
        sa.Column("review_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("superseded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("split IN ('development','holdout')", name="ck_request_following_split"),
    )
    op.create_index(
        "uq_request_following_current",
        "request_following_cases",
        ["thread_id"],
        unique=True,
        postgresql_where=sa.text("superseded = false"),
    )
    op.create_index(
        "idx_request_following_queue", "request_following_cases", ["superseded", "created_at", "id"]
    )
    op.create_index("idx_request_following_expiry", "request_following_cases", ["expires_at"])


def downgrade():
    op.drop_table("request_following_cases")
    op.drop_table("request_following_collector")
