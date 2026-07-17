"""Add Smart Captions persistence foundation and per-item choice.

Revision ID: 0065
Revises: 0064
Create Date: 2026-07-17

All new behavior remains behind SMART_CAPTIONS_ENABLED=false.  Existing plan
items receive smart_captions_enabled=false without a data backfill.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column(
            "smart_captions_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_check_constraint(
        "ck_plan_items_smart_captions_format",
        "plan_items",
        "NOT smart_captions_enabled OR COALESCE(edit_format, '') = 'subtitled'",
    )

    op.create_table(
        "creator_style_assignments",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("preset_id", sa.Text(), nullable=False),
        sa.Column("preset_version", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("assigned_by", sa.Text(), nullable=False, server_default="system"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "smart_edit_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("variant_id", sa.Text(), nullable=False),
        sa.Column("source_base_gcs_path", sa.Text(), nullable=False),
        sa.Column("source_base_sha256", sa.String(length=64), nullable=False),
        sa.Column("transcript_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("preset_id", sa.Text(), nullable=False),
        sa.Column("preset_version", sa.Text(), nullable=False),
        sa.Column("asset_pack_id", sa.Text(), nullable=False),
        sa.Column("asset_pack_version", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column(
            "normalized_words",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("face_observations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("requested_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ready_revision", sa.Integer(), nullable=True),
        sa.Column("accepted_revision", sa.Integer(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="building"),
        sa.Column(
            "supersedes_plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("smart_edit_plans.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("retired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "requested_revision >= 0",
            name="ck_smart_edit_plans_requested_revision",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(normalized_words) = 'array' AND jsonb_array_length(normalized_words) > 0",
            name="ck_smart_edit_plans_normalized_words",
        ),
        sa.CheckConstraint(
            "ready_revision IS NULL OR "
            "(ready_revision >= 0 AND ready_revision <= requested_revision)",
            name="ck_smart_edit_plans_ready_revision",
        ),
        sa.CheckConstraint(
            "accepted_revision IS NULL OR "
            "(ready_revision IS NOT NULL AND accepted_revision >= 0 "
            "AND accepted_revision <= ready_revision)",
            name="ck_smart_edit_plans_accepted_revision",
        ),
        sa.CheckConstraint(
            "state IN ('building', 'rendering', 'ready', 'rerendering', 'failed', 'retired')",
            name="ck_smart_edit_plans_state",
        ),
    )
    op.create_index(
        "uq_smart_edit_plans_active_job_variant",
        "smart_edit_plans",
        ["job_id", "variant_id"],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )
    op.create_index(
        "idx_smart_edit_plans_job_id",
        "smart_edit_plans",
        ["job_id"],
    )
    op.create_index(
        "idx_smart_edit_plans_supersedes",
        "smart_edit_plans",
        ["supersedes_plan_id"],
    )
    op.create_index(
        "idx_smart_edit_plans_user_updated",
        "smart_edit_plans",
        ["user_id", "updated_at"],
    )

    op.create_table(
        "smart_edit_plan_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("smart_edit_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=True),
        sa.Column(
            "document",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("compiled_patch", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("correction", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "planner_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("validation_receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("render_generation_id", sa.Text(), nullable=False),
        sa.Column("output_gcs_path", sa.Text(), nullable=True),
        sa.Column("output_sha256", sa.String(length=64), nullable=True),
        sa.Column("output_gcs_generation", sa.Text(), nullable=True),
        sa.Column("output_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("output_duration_ms", sa.Integer(), nullable=True),
        sa.Column("output_probe_receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("stage_artifacts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("render_receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="requested"),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("render_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("render_finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("plan_id", "revision", name="uq_smart_edit_revision_number"),
        sa.CheckConstraint(
            "revision >= 0 AND "
            "((revision = 0 AND parent_revision IS NULL) OR "
            "(revision > 0 AND parent_revision = revision - 1))",
            name="ck_smart_edit_revision_lineage",
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'rendering', 'ready', 'failed')",
            name="ck_smart_edit_revision_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(document) = 'object'",
            name="ck_smart_edit_revision_document",
        ),
        sa.CheckConstraint(
            "(revision = 0 AND correction IS NULL AND idempotency_key IS NULL) OR "
            "(revision > 0 AND correction IS NOT NULL AND idempotency_key IS NOT NULL)",
            name="ck_smart_edit_revision_correction",
        ),
    )
    op.create_index(
        "uq_smart_edit_revision_idempotency",
        "smart_edit_plan_revisions",
        ["plan_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "idx_smart_edit_revisions_plan_status",
        "smart_edit_plan_revisions",
        ["plan_id", "status"],
    )

    op.create_table(
        "smart_edit_dispatches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("render_generation_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "available_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "plan_id",
            "revision",
            "render_generation_id",
            name="uq_smart_edit_dispatch_generation",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "revision"],
            ["smart_edit_plan_revisions.plan_id", "smart_edit_plan_revisions.revision"],
            name="fk_smart_edit_dispatch_revision",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_smart_edit_dispatch_revision"),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_smart_edit_dispatch_attempt_count",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'dispatched', 'completed', 'failed', 'cancelled')",
            name="ck_smart_edit_dispatch_state",
        ),
    )
    op.create_index(
        "idx_smart_edit_dispatches_claim",
        "smart_edit_dispatches",
        ["state", "available_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_smart_edit_dispatches_claim", table_name="smart_edit_dispatches")
    op.drop_table("smart_edit_dispatches")
    op.drop_index("idx_smart_edit_revisions_plan_status", table_name="smart_edit_plan_revisions")
    op.drop_index("uq_smart_edit_revision_idempotency", table_name="smart_edit_plan_revisions")
    op.drop_table("smart_edit_plan_revisions")
    op.drop_index("idx_smart_edit_plans_user_updated", table_name="smart_edit_plans")
    op.drop_index("idx_smart_edit_plans_supersedes", table_name="smart_edit_plans")
    op.drop_index("idx_smart_edit_plans_job_id", table_name="smart_edit_plans")
    op.drop_index("uq_smart_edit_plans_active_job_variant", table_name="smart_edit_plans")
    op.drop_table("smart_edit_plans")
    op.drop_table("creator_style_assignments")
    op.drop_constraint(
        "ck_plan_items_smart_captions_format",
        "plan_items",
        type_="check",
    )
    op.drop_column("plan_items", "smart_captions_enabled")
