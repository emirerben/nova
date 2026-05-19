"""Create initial schema: users, oauth_tokens, jobs, job_clips.

Revision ID: 0000
Revises:
Create Date: 2026-01-01

These tables were created before alembic migrations were introduced.
Existing databases already have them; this migration lets fresh local
installs bootstrap without needing a manual schema dump.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, UUID

revision = "0000"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "oauth_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("access_token", BYTEA(), nullable=False),
        sa.Column("refresh_token", BYTEA(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", "platform"),
    )
    op.create_index("idx_oauth_tokens_user_platform", "oauth_tokens", ["user_id", "platform"])

    op.create_table(
        "jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("raw_storage_path", sa.Text(), nullable=False),
        sa.Column("selected_platforms", ARRAY(sa.Text()), nullable=True),
        sa.Column("probe_metadata", JSONB(), nullable=True),
        sa.Column("transcript", JSONB(), nullable=True),
        sa.Column("scene_cuts", JSONB(), nullable=True),
        sa.Column("all_candidates", JSONB(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("idx_jobs_user_id", "jobs", ["user_id"])
    op.create_index("idx_jobs_status", "jobs", ["status"])

    op.create_table(
        "job_clips",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("hook_score", sa.Float(), nullable=False),
        sa.Column("engagement_score", sa.Float(), nullable=False),
        sa.Column("combined_score", sa.Float(), nullable=False),
        sa.Column("start_s", sa.Float(), nullable=False),
        sa.Column("end_s", sa.Float(), nullable=False),
        sa.Column("hook_text", sa.Text(), nullable=True),
        sa.Column("platform_copy", JSONB(), nullable=True),
        sa.Column("copy_status", sa.Text(), nullable=False, server_default="generated"),
        sa.Column("video_path", sa.Text(), nullable=True),
        sa.Column("thumbnail_path", sa.Text(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("render_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("post_status", JSONB(), nullable=True),
        sa.Column("download_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("idx_job_clips_job_id", "job_clips", ["job_id"])
    op.create_index("idx_job_clips_rank", "job_clips", ["job_id", "rank"])


def downgrade() -> None:
    op.drop_table("job_clips")
    op.drop_table("jobs")
    op.drop_table("oauth_tokens")
    op.drop_table("users")
