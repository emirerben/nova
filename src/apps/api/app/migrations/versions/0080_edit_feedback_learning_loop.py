"""Add the consent-safe edit-feedback learning-loop data foundation.

The tables in this migration are identity and policy records only.  They do
not retain source clips, base videos, intermediate renders, or signed URLs.
Artifact storage paths are generation-pinned and deliberately use the
creator-owned prefix so account deletion can sweep them.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def _uuid(name: str, **kwargs):
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        **kwargs,
    )


def upgrade() -> None:
    op.create_table(
        "internal_account_grants",
        _uuid("id", primary_key=True),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("granted_by", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "effective_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_internal_account_grants_status",
        ),
        sa.UniqueConstraint(
            "creator_id",
            "idempotency_key",
            name="uq_internal_account_grants_idempotency",
        ),
    )
    op.create_index(
        "uq_internal_account_grants_active_creator",
        "internal_account_grants",
        ["creator_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "training_consent_events",
        _uuid("id", primary_key=True),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), server_default="creator", nullable=False),
        sa.Column(
            "effective_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "revokes_consent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("training_consent_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "purpose IN ('edit_feedback_training')",
            name="ck_training_consent_events_purpose",
        ),
        sa.CheckConstraint(
            "action IN ('grant', 'revoke')",
            name="ck_training_consent_events_action",
        ),
        sa.UniqueConstraint(
            "creator_id", "idempotency_key", name="uq_training_consent_events_idempotency"
        ),
    )
    op.create_index(
        "idx_training_consent_events_creator_purpose_effective",
        "training_consent_events",
        ["creator_id", "purpose", "effective_at"],
    )

    op.create_table(
        "edit_artifacts",
        _uuid("id", primary_key=True),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "parent_artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("edit_artifacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("variant_id", sa.Text(), nullable=True),
        sa.Column("render_generation_id", sa.Text(), nullable=False),
        sa.Column("artifact_kind", sa.Text(), nullable=False),
        sa.Column("artifact_schema_version", sa.Text(), server_default="1", nullable=False),
        sa.Column("proposal_version", sa.Text(), nullable=True),
        sa.Column("media_digest", sa.Text(), nullable=True),
        sa.Column("direction_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("render_hash", sa.Text(), nullable=False),
        sa.Column("render_receipt_hash", sa.Text(), nullable=False),
        sa.Column("render_receipt_schema_version", sa.Text(), nullable=True),
        sa.Column("render_receipt", postgresql.JSONB(), nullable=False),
        sa.Column("prompt_id", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("prompt_hash", sa.Text(), nullable=True),
        sa.Column("requested_model", sa.Text(), nullable=True),
        sa.Column("effective_model", sa.Text(), nullable=True),
        sa.Column("model_provider", sa.Text(), nullable=True),
        sa.Column("media_manifest", postgresql.JSONB(), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("storage_generation", sa.Text(), nullable=False),
        sa.Column("storage_content_hash", sa.Text(), nullable=False),
        sa.Column("storage_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("capture_origin", sa.Text(), nullable=False),
        sa.Column("eligibility_basis", sa.Text(), nullable=False),
        sa.Column(
            "consent_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("training_consent_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "internal_grant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("internal_account_grants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("creator_split", sa.Text(), nullable=False),
        sa.Column("plan_item_split", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "artifact_kind IN ('final_render', 'poster', 'contact_sheet')",
            name="ck_edit_artifacts_kind",
        ),
        sa.CheckConstraint(
            "capture_origin IN ('creator', 'internal', 'admin', 'system')",
            name="ck_edit_artifacts_capture_origin",
        ),
        sa.CheckConstraint(
            "eligibility_basis IN ('internal_grant', 'training_consent')",
            name="ck_edit_artifacts_eligibility_basis",
        ),
        sa.CheckConstraint(
            "creator_split IN ('train', 'validation', 'test', 'holdout')",
            name="ck_edit_artifacts_creator_split",
        ),
        sa.CheckConstraint(
            "plan_item_split IN ('train', 'validation', 'test', 'holdout')",
            name="ck_edit_artifacts_plan_item_split",
        ),
        sa.UniqueConstraint(
            "storage_path", "storage_generation", name="uq_edit_artifacts_storage_identity"
        ),
    )
    op.create_index(
        "idx_edit_artifacts_creator_created", "edit_artifacts", ["creator_id", "created_at"]
    )
    op.create_index(
        "idx_edit_artifacts_plan_item_created", "edit_artifacts", ["plan_item_id", "created_at"]
    )
    op.create_index(
        "idx_edit_artifacts_kind_created", "edit_artifacts", ["artifact_kind", "created_at"]
    )
    op.create_index(
        "idx_edit_artifacts_split", "edit_artifacts", ["creator_split", "plan_item_split"]
    )

    op.create_table(
        "edit_interaction_receipts",
        _uuid("id", primary_key=True),
        sa.Column("event_kind", sa.Text(), nullable=False),
        sa.Column(
            "proposal_receipt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("edit_interaction_receipts.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("variant_id", sa.Text(), nullable=False),
        sa.Column("client_event_id", sa.Text(), nullable=True),
        sa.Column("utterance", sa.Text(), nullable=False),
        sa.Column("inferred_intent", sa.Text(), nullable=False),
        sa.Column("model_reply", sa.Text(), nullable=False),
        sa.Column("eligibility_basis", sa.Text(), nullable=False),
        sa.Column(
            "consent_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("training_consent_events.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "internal_grant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("internal_account_grants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("proposed_operations", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("proposed_operations_digest", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("proposal_outcome", sa.Text(), nullable=False),
        sa.Column("execution_outcome", sa.Text(), nullable=True),
        sa.Column("rejection_reasons", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("before_revision_hash", sa.Text(), nullable=True),
        sa.Column("after_revision_hash", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_kind IN ('proposal', 'execution', 'save_link')",
            name="ck_edit_interaction_receipts_event_kind",
        ),
        sa.CheckConstraint(
            "proposal_outcome IN ('applied', 'clarification', 'no_effect', "
            "'unsupported', 'stale', 'failed')",
            name="ck_edit_interaction_receipts_proposal_outcome",
        ),
        sa.CheckConstraint(
            "execution_outcome IS NULL OR execution_outcome IN "
            "('applied', 'no_effect', 'rejected', 'stale', 'failed')",
            name="ck_edit_interaction_receipts_execution_outcome",
        ),
        sa.CheckConstraint(
            "(eligibility_basis = 'training_consent' AND consent_event_id IS NOT NULL "
            "AND internal_grant_id IS NULL) OR "
            "(eligibility_basis = 'internal_grant' AND internal_grant_id IS NOT NULL "
            "AND consent_event_id IS NULL)",
            name="ck_edit_interaction_receipts_eligibility",
        ),
        sa.CheckConstraint(
            "(event_kind = 'proposal' AND proposal_receipt_id IS NULL "
            "AND client_event_id IS NULL AND execution_outcome IS NULL) OR "
            "(event_kind IN ('execution', 'save_link') AND proposal_receipt_id IS NOT NULL "
            "AND client_event_id IS NOT NULL AND execution_outcome IS NOT NULL)",
            name="ck_edit_interaction_receipts_event_shape",
        ),
        sa.UniqueConstraint(
            "creator_id", "client_event_id", name="uq_edit_interaction_receipts_creator_event"
        ),
    )
    op.create_index(
        "idx_edit_interaction_receipts_proposal_created",
        "edit_interaction_receipts",
        ["proposal_receipt_id", "created_at"],
    )
    op.create_index(
        "idx_edit_interaction_receipts_plan_item_created",
        "edit_interaction_receipts",
        ["plan_item_id", "created_at"],
    )

    op.create_table(
        "edit_feedback_annotations",
        _uuid("id", primary_key=True),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("edit_artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("rating", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("frame_start_ms", sa.Integer(), nullable=True),
        sa.Column("frame_end_ms", sa.Integer(), nullable=True),
        sa.Column("target", postgresql.JSONB(), nullable=True),
        sa.Column("reviewer_identity", sa.Text(), nullable=False),
        sa.Column(
            "supersedes_annotation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("edit_feedback_annotations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "dimension IN ('overall_quality', 'ai_guidance_and_response', "
            "'instruction_fit', 'hook', 'pacing', 'cuts', 'clip_selection', "
            "'clip_ordering', 'captions', 'text', 'transitions', 'music', "
            "'audio', 'effects', 'overlays')",
            name="ck_edit_feedback_annotations_dimension",
        ),
        sa.CheckConstraint(
            "rating IN ('good', 'bad', 'mixed', 'not_applicable')",
            name="ck_edit_feedback_annotations_rating",
        ),
        sa.CheckConstraint(
            "frame_start_ms IS NULL OR frame_start_ms >= 0",
            name="ck_edit_feedback_annotations_frame_start",
        ),
        sa.CheckConstraint(
            "frame_end_ms IS NULL OR frame_end_ms >= 0",
            name="ck_edit_feedback_annotations_frame_end",
        ),
        sa.CheckConstraint(
            "frame_start_ms IS NULL OR frame_end_ms IS NULL OR frame_end_ms >= frame_start_ms",
            name="ck_edit_feedback_annotations_frame_order",
        ),
        sa.CheckConstraint(
            "rating = 'not_applicable' OR (rationale IS NOT NULL AND length(trim(rationale)) > 0)",
            name="ck_edit_feedback_annotations_rationale",
        ),
    )
    op.create_index(
        "idx_edit_feedback_annotations_creator_created",
        "edit_feedback_annotations",
        ["creator_id", "created_at"],
    )
    op.create_index(
        "idx_edit_feedback_annotations_plan_item_created",
        "edit_feedback_annotations",
        ["plan_item_id", "created_at"],
    )
    op.create_index(
        "idx_edit_feedback_annotations_artifact_created",
        "edit_feedback_annotations",
        ["artifact_id", "created_at"],
    )

    op.create_table(
        "training_artifact_retention_events",
        _uuid("id", primary_key=True),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("edit_artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("storage_generation", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "event_type IN ('copy', 'purge', 'build', 'ready', 'failed')",
            name="ck_training_retention_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'started', 'succeeded', 'failed')",
            name="ck_training_retention_event_status",
        ),
        sa.UniqueConstraint(
            "artifact_id", "idempotency_key", name="uq_training_retention_event_idempotency"
        ),
    )
    op.create_index(
        "idx_training_retention_events_artifact_created",
        "training_artifact_retention_events",
        ["artifact_id", "created_at"],
    )
    op.create_index(
        "idx_training_retention_events_creator_created",
        "training_artifact_retention_events",
        ["creator_id", "created_at"],
    )

    op.create_table(
        "training_dataset_exports",
        _uuid("id", primary_key=True),
        sa.Column(
            "requested_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("dataset_schema_version", sa.Text(), server_default="1", nullable=False),
        sa.Column("export_format", sa.Text(), server_default="jsonl", nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("creator_split_version", sa.Text(), nullable=False),
        sa.Column("plan_item_split_version", sa.Text(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("manifest", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("storage_generation", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
            "status IN ('pending', 'building', 'ready', 'failed', 'revoked')",
            name="ck_training_dataset_exports_status",
        ),
        sa.CheckConstraint(
            "export_format IN ('jsonl', 'parquet')",
            name="ck_training_dataset_exports_format",
        ),
        sa.UniqueConstraint(
            "requested_by", "idempotency_key", name="uq_training_dataset_exports_idempotency"
        ),
    )
    op.create_index(
        "idx_training_dataset_exports_status_created",
        "training_dataset_exports",
        ["status", "created_at"],
    )
    op.create_index(
        "idx_training_dataset_exports_requested_by_created",
        "training_dataset_exports",
        ["requested_by", "created_at"],
    )

    # Core learning evidence is correction-by-append. DELETE remains available
    # only because account-erasure cascades must remove a creator's audit rows.
    op.execute(
        """
        CREATE FUNCTION reject_edit_learning_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'edit learning records are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in (
        "training_consent_events",
        "edit_artifacts",
        "edit_interaction_receipts",
        "edit_feedback_annotations",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE ON {table} FOR EACH ROW "
            "EXECUTE FUNCTION reject_edit_learning_update()"
        )


def downgrade() -> None:
    for table in (
        "training_consent_events",
        "edit_artifacts",
        "edit_interaction_receipts",
        "edit_feedback_annotations",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_edit_learning_update()")
    op.drop_index(
        "idx_training_dataset_exports_requested_by_created",
        table_name="training_dataset_exports",
    )
    op.drop_index(
        "idx_training_dataset_exports_status_created", table_name="training_dataset_exports"
    )
    op.drop_table("training_dataset_exports")

    op.drop_index(
        "idx_training_retention_events_creator_created",
        table_name="training_artifact_retention_events",
    )
    op.drop_index(
        "idx_training_retention_events_artifact_created",
        table_name="training_artifact_retention_events",
    )
    op.drop_table("training_artifact_retention_events")

    op.drop_index(
        "idx_edit_feedback_annotations_artifact_created",
        table_name="edit_feedback_annotations",
    )
    op.drop_index(
        "idx_edit_feedback_annotations_plan_item_created",
        table_name="edit_feedback_annotations",
    )
    op.drop_index(
        "idx_edit_feedback_annotations_creator_created",
        table_name="edit_feedback_annotations",
    )
    op.drop_table("edit_feedback_annotations")

    op.drop_index(
        "idx_edit_interaction_receipts_plan_item_created",
        table_name="edit_interaction_receipts",
    )
    op.drop_index(
        "idx_edit_interaction_receipts_proposal_created",
        table_name="edit_interaction_receipts",
    )
    op.drop_table("edit_interaction_receipts")

    op.drop_index("idx_edit_artifacts_split", table_name="edit_artifacts")
    op.drop_index("idx_edit_artifacts_kind_created", table_name="edit_artifacts")
    op.drop_index("idx_edit_artifacts_plan_item_created", table_name="edit_artifacts")
    op.drop_index("idx_edit_artifacts_creator_created", table_name="edit_artifacts")
    op.drop_table("edit_artifacts")

    op.drop_index(
        "idx_training_consent_events_creator_purpose_effective",
        table_name="training_consent_events",
    )
    op.drop_table("training_consent_events")

    op.drop_index("uq_internal_account_grants_active_creator", table_name="internal_account_grants")
    op.drop_table("internal_account_grants")
