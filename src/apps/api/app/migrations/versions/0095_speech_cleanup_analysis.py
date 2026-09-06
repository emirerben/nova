"""Add generation-pinned speech-cleanup preflight analyses.

Revision ID: 0095
Revises: 0094
Create Date: 2026-09-05

The analysis table is the durable idempotency and CAS boundary for chat-first
speech cleanup. Private timed-word/CutPlan evidence is kept separate from the
bounded result columns that may be projected to a creator.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column("voiceover_generation", sa.Text(), nullable=True),
    )
    op.add_column(
        "plan_items",
        sa.Column("voiceover_duration_s", sa.Float(), nullable=True),
    )

    op.create_table(
        "speech_cleanup_analyses",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "plan_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_media_identity", sa.Text(), nullable=False),
        sa.Column("source_storage_path", sa.Text(), nullable=False),
        sa.Column("source_generation", sa.Text(), nullable=False),
        sa.Column("window_start_s", sa.Float(), server_default="0", nullable=False),
        sa.Column("window_end_s", sa.Float(), nullable=True),
        sa.Column("source_policy_fingerprint", sa.Text(), nullable=False),
        sa.Column("engine_version", sa.Text(), nullable=False),
        sa.Column("detector_version", sa.Text(), nullable=False),
        sa.Column(
            "analysis_payload_version",
            sa.Text(),
            server_default="1",
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("attempt_token", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("dispatched_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_dispatch_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("analysis_payload", postgresql.JSONB(), nullable=True),
        sa.Column("candidate_count", sa.Integer(), nullable=True),
        sa.Column("category_counts", postgresql.JSONB(), nullable=True),
        sa.Column("estimated_removed_ms", sa.Integer(), nullable=True),
        sa.Column("diagnostic_receipt", postgresql.JSONB(), nullable=True),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("failure_retryable", sa.Boolean(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("decision_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
            "source_kind IN ('voiceover', 'embedded_spine')",
            name="ck_speech_cleanup_analyses_source_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'ready', 'no_findings', 'failed')",
            name="ck_speech_cleanup_analyses_status",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('clean', 'keep_original', 'create_without_cleanup')",
            name="ck_speech_cleanup_analyses_decision",
        ),
        sa.CheckConstraint(
            "window_start_s >= 0 AND (window_end_s IS NULL OR window_end_s > window_start_s)",
            name="ck_speech_cleanup_analyses_window",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_speech_cleanup_analyses_attempt_count",
        ),
        sa.CheckConstraint(
            "candidate_count IS NULL OR candidate_count >= 0",
            name="ck_speech_cleanup_analyses_candidate_count",
        ),
        sa.CheckConstraint(
            "estimated_removed_ms IS NULL OR estimated_removed_ms >= 0",
            name="ck_speech_cleanup_analyses_removed_ms",
        ),
        sa.CheckConstraint(
            "(decision IS NULL AND decision_at IS NULL) OR "
            "(decision IS NOT NULL AND decision_at IS NOT NULL)",
            name="ck_speech_cleanup_analyses_decision_at",
        ),
    )

    op.create_index(
        "uq_speech_cleanup_analysis_identity",
        "speech_cleanup_analyses",
        ["plan_item_id", "source_policy_fingerprint", "engine_version"],
        unique=True,
    )
    op.create_index(
        "uq_speech_cleanup_analysis_current",
        "speech_cleanup_analyses",
        ["plan_item_id"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index(
        "idx_speech_cleanup_analysis_dispatch",
        "speech_cleanup_analyses",
        ["next_dispatch_at", "created_at"],
        postgresql_where=sa.text(
            "status = 'queued' AND dispatched_at IS NULL "
            "AND next_dispatch_at IS NOT NULL AND superseded_at IS NULL"
        ),
    )
    op.create_index(
        "idx_speech_cleanup_analysis_lease",
        "speech_cleanup_analyses",
        ["lease_expires_at", "id"],
        postgresql_where=sa.text(
            "status IN ('queued', 'running') AND lease_expires_at IS NOT NULL "
            "AND superseded_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_speech_cleanup_analysis_lease",
        table_name="speech_cleanup_analyses",
    )
    op.drop_index(
        "idx_speech_cleanup_analysis_dispatch",
        table_name="speech_cleanup_analyses",
    )
    op.drop_index(
        "uq_speech_cleanup_analysis_current",
        table_name="speech_cleanup_analyses",
    )
    op.drop_index(
        "uq_speech_cleanup_analysis_identity",
        table_name="speech_cleanup_analyses",
    )
    op.drop_table("speech_cleanup_analyses")
    op.drop_column("plan_items", "voiceover_duration_s")
    op.drop_column("plan_items", "voiceover_generation")
