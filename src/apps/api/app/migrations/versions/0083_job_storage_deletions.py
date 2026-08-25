"""Add a durable outbox for per-job storage deletion manifests."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_storage_deletions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        # Deliberately no FK: the source Job is deleted in the same transaction
        # and this manifest must survive that deletion.
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_paths", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("lease_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("job_id", name="uq_job_storage_deletions_job_id"),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed')",
            name="ck_job_storage_deletions_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_job_storage_deletions_attempts_nonnegative",
        ),
    )
    op.create_index(
        "idx_job_storage_deletions_due",
        "job_storage_deletions",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "idx_job_storage_deletions_lease",
        "job_storage_deletions",
        ["status", "lease_until"],
    )
    op.create_index(
        "idx_job_storage_deletions_completed",
        "job_storage_deletions",
        ["completed_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_job_storage_deletions_completed", table_name="job_storage_deletions")
    op.drop_index("idx_job_storage_deletions_lease", table_name="job_storage_deletions")
    op.drop_index("idx_job_storage_deletions_due", table_name="job_storage_deletions")
    op.drop_table("job_storage_deletions")
