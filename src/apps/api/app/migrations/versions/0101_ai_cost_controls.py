"""Add atomic AI cost controls, durable review/cache rows, and rich metering.

Revision ID: 0101
Revises: 0100
Create Date: 2026-09-08

The migration is expand-only. Existing call paths remain unchanged until
AI_COST_CONTROL_ENABLED is enabled after split-project credentials and budgets
have been configured.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


def _uuid(name: str, *args, **kwargs) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, **kwargs)


def upgrade() -> None:
    op.create_table(
        "ai_cost_reservations",
        _uuid("id", server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("usage_purpose", sa.Text(), nullable=False),
        sa.Column("feature", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), server_default="google", nullable=False),
        sa.Column("requested_model", sa.Text(), nullable=False),
        sa.Column("resolved_model", sa.Text(), nullable=True),
        _uuid("creator_id", sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _uuid("job_id", sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        _uuid(
            "creator_agent_session_id",
            sa.ForeignKey("creator_agent_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("principal_type", sa.Text(), server_default="system", nullable=False),
        sa.Column("test_run_id", sa.Text(), nullable=True),
        sa.Column("release_canary_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="reserved", nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("settled_cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("price_version", sa.Text(), nullable=False),
        sa.Column("provider_request_id", sa.Text(), nullable=True),
        sa.Column("usage_json", postgresql.JSONB(), nullable=True),
        sa.Column("period_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("settled_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('reserved','provider_started','settled','released','unknown','rejected')",
            name="ck_ai_cost_reservations_status",
        ),
        sa.CheckConstraint(
            "principal_type IN ('customer','internal','system')",
            name="ck_ai_cost_reservations_principal_type",
        ),
        sa.CheckConstraint(
            "estimated_cost_usd >= 0 AND (settled_cost_usd IS NULL OR settled_cost_usd >= 0)",
            name="ck_ai_cost_reservations_costs_nonnegative",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_ai_cost_reservations_idempotency"),
    )
    op.create_index(
        "idx_ai_cost_reservations_budget",
        "ai_cost_reservations",
        ["environment", "period_start", "status"],
    )
    op.create_index(
        "idx_ai_cost_reservations_purpose",
        "ai_cost_reservations",
        ["usage_purpose", "period_start", "status"],
    )
    op.create_index(
        "idx_ai_cost_reservations_creator_day",
        "ai_cost_reservations",
        ["creator_id", "feature", "created_at"],
    )
    op.create_index(
        "idx_ai_cost_reservations_test_run",
        "ai_cost_reservations",
        ["test_run_id", "created_at"],
    )
    op.create_index("idx_ai_cost_reservations_job", "ai_cost_reservations", ["job_id"])
    op.create_index(
        "idx_ai_cost_reservations_session",
        "ai_cost_reservations",
        ["creator_agent_session_id"],
    )
    op.create_index(
        "idx_ai_cost_reservations_settled_at",
        "ai_cost_reservations",
        ["settled_at"],
        postgresql_where=sa.text("status = 'settled'"),
    )

    op.create_table(
        "ai_budget_overrides",
        _uuid("id", server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("additional_cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("additional_cost_usd > 0", name="ck_ai_budget_overrides_positive"),
    )
    op.create_index(
        "idx_ai_budget_overrides_active",
        "ai_budget_overrides",
        ["scope", "expires_at"],
    )

    op.create_table(
        "director_review_cache",
        _uuid("id", server_default=sa.text("gen_random_uuid()"), primary_key=True),
        _uuid("creator_id", sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        _uuid("job_id", sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_revision", sa.Text(), nullable=False),
        sa.Column("snapshot_digest", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("response_json", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "creator_id",
            "snapshot_digest",
            "prompt_version",
            "model",
            name="uq_director_review_cache_identity",
        ),
    )
    op.create_index("idx_director_review_cache_expiry", "director_review_cache", ["expires_at"])
    op.create_index("idx_director_review_cache_job", "director_review_cache", ["job_id"])

    op.create_table(
        "media_analysis_cache",
        _uuid("id", server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("source_identity", sa.Text(), nullable=False),
        sa.Column("analyzer", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("result_json", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_hit_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("hit_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.UniqueConstraint(
            "source_identity",
            "analyzer",
            "model",
            "prompt_version",
            "schema_version",
            name="uq_media_analysis_cache_identity",
        ),
    )
    op.create_index("idx_media_analysis_cache_expiry", "media_analysis_cache", ["expires_at"])

    op.create_table(
        "storage_retention_manifests",
        _uuid("id", server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("status", sa.Text(), server_default="report_only", nullable=False),
        sa.Column("generated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("report_only_until", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        _uuid("execution_lease_id", nullable=True),
        sa.Column("execution_lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("summary_json", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('report_only','pending_approval','approved','executing',"
            "'completed','rejected')",
            name="ck_storage_retention_manifest_status",
        ),
    )
    op.create_index(
        "idx_storage_retention_manifest_status",
        "storage_retention_manifests",
        ["status", "generated_at"],
    )
    op.create_table(
        "storage_retention_entries",
        _uuid("id", server_default=sa.text("gen_random_uuid()"), primary_key=True),
        _uuid(
            "manifest_id",
            sa.ForeignKey("storage_retention_manifests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _uuid("job_id", nullable=True),
        _uuid("creator_id", nullable=True),
        sa.Column("object_path", sa.Text(), nullable=False),
        sa.Column("object_generation", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("eligible_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("action IN ('warn','delete')", name="ck_storage_retention_entry_action"),
        sa.CheckConstraint(
            "status IN ('pending','deleted','skipped','failed')",
            name="ck_storage_retention_entry_status",
        ),
        sa.UniqueConstraint(
            "manifest_id",
            "object_path",
            "object_generation",
            name="uq_storage_retention_entry_object",
        ),
    )
    op.create_index(
        "idx_storage_retention_entry_manifest",
        "storage_retention_entries",
        ["manifest_id", "action", "status"],
    )
    op.create_index(
        "idx_storage_retention_entry_creator_warning",
        "storage_retention_entries",
        ["creator_id", "action", "status", "job_id"],
    )

    op.create_table(
        "billing_reconciliations",
        _uuid("id", server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("threshold_pct", sa.Numeric(8, 6), nullable=False),
        sa.Column("cloud_costs_usd", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("ledger_costs_usd", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("differences", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("export_watermark", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("alerted_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('matched','mismatch','incomplete','failed')",
            name="ck_billing_reconciliations_status",
        ),
        sa.UniqueConstraint("usage_date", name="uq_billing_reconciliations_usage_date"),
    )
    op.create_index(
        "idx_billing_reconciliations_status",
        "billing_reconciliations",
        ["status", "usage_date"],
    )

    for name, column_type in (
        ("tokens_thoughts", sa.Integer()),
        ("tokens_cached", sa.Integer()),
        ("tokens_tool", sa.Integer()),
        ("token_details", postgresql.JSONB()),
        ("environment", sa.Text()),
        ("usage_purpose", sa.Text()),
        ("test_run_id", sa.Text()),
        ("provider_request_id", sa.Text()),
        ("price_version", sa.Text()),
        ("reserved_cost_usd", sa.Numeric(12, 6)),
        ("settled_cost_usd", sa.Numeric(12, 6)),
    ):
        op.add_column("agent_run", sa.Column(name, column_type, nullable=True))
    op.add_column("agent_run", _uuid("cost_reservation_id", nullable=True))
    op.add_column("personas", _uuid("tiktok_style_analysis_claim_id", nullable=True))
    op.add_column(
        "personas",
        sa.Column(
            "tiktok_style_analysis_claim_expires_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    populated = bind.execute(
        sa.text(
            """
            SELECT
              EXISTS (SELECT 1 FROM ai_cost_reservations) OR
              EXISTS (SELECT 1 FROM ai_budget_overrides) OR
              EXISTS (SELECT 1 FROM director_review_cache) OR
              EXISTS (SELECT 1 FROM media_analysis_cache) OR
              EXISTS (SELECT 1 FROM billing_reconciliations) OR
              EXISTS (SELECT 1 FROM storage_retention_manifests) OR
              EXISTS (
                SELECT 1 FROM personas
                WHERE tiktok_style_analysis_claim_id IS NOT NULL
                   OR tiktok_style_analysis_claim_expires_at IS NOT NULL
              ) OR
              EXISTS (
                SELECT 1 FROM agent_run
                WHERE cost_reservation_id IS NOT NULL
                   OR tokens_thoughts IS NOT NULL
                   OR tokens_cached IS NOT NULL
                   OR tokens_tool IS NOT NULL
                   OR token_details IS NOT NULL
                   OR environment IS NOT NULL
                   OR usage_purpose IS NOT NULL
                   OR test_run_id IS NOT NULL
                   OR provider_request_id IS NOT NULL
                   OR price_version IS NOT NULL
                   OR reserved_cost_usd IS NOT NULL
                   OR settled_cost_usd IS NOT NULL
              )
            """
        )
    ).scalar_one()
    if populated:
        raise RuntimeError(
            "0101 contains durable cost-control or retention data; disable writers "
            "and roll application code back without downgrading the schema"
        )

    op.drop_column("personas", "tiktok_style_analysis_claim_expires_at")
    op.drop_column("personas", "tiktok_style_analysis_claim_id")
    for name in (
        "cost_reservation_id",
        "settled_cost_usd",
        "reserved_cost_usd",
        "price_version",
        "provider_request_id",
        "test_run_id",
        "usage_purpose",
        "environment",
        "token_details",
        "tokens_tool",
        "tokens_cached",
        "tokens_thoughts",
    ):
        op.drop_column("agent_run", name)

    op.drop_index("idx_billing_reconciliations_status", table_name="billing_reconciliations")
    op.drop_table("billing_reconciliations")
    op.drop_index("idx_media_analysis_cache_expiry", table_name="media_analysis_cache")
    op.drop_table("media_analysis_cache")
    op.drop_index("idx_storage_retention_entry_manifest", table_name="storage_retention_entries")
    op.drop_index(
        "idx_storage_retention_entry_creator_warning",
        table_name="storage_retention_entries",
    )
    op.drop_table("storage_retention_entries")
    op.drop_index("idx_storage_retention_manifest_status", table_name="storage_retention_manifests")
    op.drop_table("storage_retention_manifests")
    op.drop_index("idx_director_review_cache_job", table_name="director_review_cache")
    op.drop_index("idx_director_review_cache_expiry", table_name="director_review_cache")
    op.drop_table("director_review_cache")
    op.drop_index("idx_ai_budget_overrides_active", table_name="ai_budget_overrides")
    op.drop_table("ai_budget_overrides")
    op.drop_index("idx_ai_cost_reservations_session", table_name="ai_cost_reservations")
    op.drop_index("idx_ai_cost_reservations_job", table_name="ai_cost_reservations")
    op.drop_index("idx_ai_cost_reservations_settled_at", table_name="ai_cost_reservations")
    op.drop_index("idx_ai_cost_reservations_test_run", table_name="ai_cost_reservations")
    op.drop_index("idx_ai_cost_reservations_creator_day", table_name="ai_cost_reservations")
    op.drop_index("idx_ai_cost_reservations_purpose", table_name="ai_cost_reservations")
    op.drop_index("idx_ai_cost_reservations_budget", table_name="ai_cost_reservations")
    op.drop_table("ai_cost_reservations")
