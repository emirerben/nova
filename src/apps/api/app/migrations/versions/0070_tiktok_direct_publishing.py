"""Add TikTok Direct Post OAuth metadata and publication lifecycle.

Revision ID: 0070
Revises: 0069
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("oauth_tokens", "access_token", existing_type=postgresql.BYTEA(), nullable=True)
    op.add_column("oauth_tokens", sa.Column("refresh_expires_at", sa.TIMESTAMP(timezone=True)))
    op.add_column("oauth_tokens", sa.Column("platform_account_id", sa.Text()))
    op.add_column("oauth_tokens", sa.Column("scopes", postgresql.JSONB()))
    op.add_column("oauth_tokens", sa.Column("account_metadata", postgresql.JSONB()))
    op.add_column("oauth_tokens", sa.Column("last_synced_at", sa.TIMESTAMP(timezone=True)))
    op.add_column("oauth_tokens", sa.Column("sync_lease_expires_at", sa.TIMESTAMP(timezone=True)))
    op.create_index(
        "uq_oauth_tokens_platform_account",
        "oauth_tokens",
        ["platform", "platform_account_id"],
        unique=True,
        postgresql_where=sa.text("platform_account_id IS NOT NULL AND status = 'active'"),
    )

    op.create_table(
        "tiktok_publications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=False
        ),
        sa.Column("variant_id", sa.Text()),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("source_object_path", sa.Text(), nullable=False),
        sa.Column("source_generation", sa.Text(), nullable=False),
        sa.Column("source_etag", sa.Text()),
        sa.Column("snapshot_object_path", sa.Text()),
        sa.Column("edit_signature", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("edit_signature_version", sa.Text(), nullable=False, server_default="1"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("privacy_level", sa.Text(), nullable=False),
        sa.Column("allow_comment", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allow_duet", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allow_stitch", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("brand_content_toggle", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("brand_organic_toggle", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_aigc", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("music_usage_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("consent_version", sa.Text(), nullable=False),
        sa.Column("consented_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("creator_info_snapshot", postgresql.JSONB()),
        sa.Column("processing_status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("visibility_status", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("tiktok_publish_id", sa.Text()),
        sa.Column("tiktok_post_id", sa.Text()),
        sa.Column("media_token_hash", sa.Text()),
        sa.Column("media_expires_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("public_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("next_poll_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("failure_code", sa.Text()),
        sa.Column("failure_detail", sa.Text()),
        sa.Column("latest_metrics", postgresql.JSONB()),
        sa.Column("metrics_synced_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("evaluation_metrics", postgresql.JSONB()),
        sa.Column("evaluation_captured_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_tiktok_pub_user_idempotency"),
        sa.UniqueConstraint("tiktok_publish_id", name="uq_tiktok_pub_publish_id"),
        sa.CheckConstraint(
            "processing_status IN ("
            "'queued','snapshotting','submitting','processing','complete',"
            "'submission_unknown','failed')",
            name="ck_tiktok_pub_processing_status",
        ),
        sa.CheckConstraint(
            "visibility_status IN ('unknown','private','public','removed')",
            name="ck_tiktok_pub_visibility_status",
        ),
    )
    op.create_index("idx_tiktok_pub_user_created", "tiktok_publications", ["user_id", "created_at"])
    op.create_index("idx_tiktok_pub_user_job", "tiktok_publications", ["user_id", "job_id"])
    op.create_index(
        "idx_tiktok_pub_due_poll", "tiktok_publications", ["processing_status", "next_poll_at"]
    )
    op.create_index("idx_tiktok_pub_post_id", "tiktok_publications", ["tiktok_post_id"])


def downgrade() -> None:
    retained_audits = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM tiktok_publications "
                "WHERE consented_at >= now() - interval '30 days'"
            )
        )
        .scalar_one()
    )
    if retained_audits:
        raise RuntimeError(
            "Cannot downgrade 0070 while TikTok consent audit records are inside retention"
        )
    op.drop_table("tiktok_publications")
    op.drop_index("uq_oauth_tokens_platform_account", table_name="oauth_tokens")
    # Revoked TikTok connections deliberately cryptographically erase their
    # access token. They cannot be represented by the pre-0070 NOT NULL
    # schema, so discard only those inert rows before restoring the constraint.
    op.execute(
        "DELETE FROM oauth_tokens WHERE platform = 'tiktok' "
        "AND status = 'revoked' AND access_token IS NULL"
    )
    remaining_null_tokens = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM oauth_tokens WHERE access_token IS NULL"))
        .scalar_one()
    )
    if remaining_null_tokens:
        raise RuntimeError(
            "Cannot restore oauth_tokens.access_token NOT NULL while other NULL tokens exist"
        )
    for column in (
        "sync_lease_expires_at",
        "last_synced_at",
        "account_metadata",
        "scopes",
        "platform_account_id",
        "refresh_expires_at",
    ):
        op.drop_column("oauth_tokens", column)
    op.alter_column(
        "oauth_tokens", "access_token", existing_type=postgresql.BYTEA(), nullable=False
    )
