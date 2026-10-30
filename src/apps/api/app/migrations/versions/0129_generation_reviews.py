"""Durable canonical runtime generation evidence outbox."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "idx_creator_executions_pending_review_admission",
        "creator_agent_executions",
        ["completed_at"],
        postgresql_where=sa.text(
            "status = 'completed' AND result->'intent_review_admission'->>'status' = 'pending'"
        ),
    )
    op.create_table(
        "creator_generation_reviews",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "execution_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("creator_agent_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("review_key", sa.Text, nullable=False, unique=True),
        sa.Column("input_digest", sa.Text, nullable=False),
        sa.Column("inputs", pg.JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("lease_token", pg.UUID(as_uuid=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_publish_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.Text),
        sa.Column("result", pg.JSONB),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','evidence_ready','unavailable','stale','exhausted')",
            name="ck_generation_review_status",
        ),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 3", name="ck_generation_review_attempts"),
        sa.CheckConstraint(
            "octet_length(inputs::text) <= 2097152", name="ck_generation_review_inputs"
        ),
        sa.CheckConstraint(
            "result IS NULL OR octet_length(result::text) <= 4194304",
            name="ck_generation_review_result",
        ),
    )
    op.create_index(
        "ix_generation_review_pending", "creator_generation_reviews", ["status", "lease_until"]
    )
    op.create_index(
        "ix_generation_review_execution", "creator_generation_reviews", ["execution_id"]
    )
    op.create_index("ix_generation_review_owner", "creator_generation_reviews", ["owner_id"])
    op.create_index(
        "ix_generation_review_publish_order",
        "creator_generation_reviews",
        ["last_publish_attempt_at", "created_at"],
    )


def downgrade():
    op.drop_index("idx_creator_executions_pending_review_admission", "creator_agent_executions")
    op.drop_table("creator_generation_reviews")
