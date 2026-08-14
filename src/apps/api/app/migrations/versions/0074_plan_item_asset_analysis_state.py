"""Persist actionable plan-item asset analysis state.

Revision ID: 0074
Revises: 0073
Create Date: 2026-08-14

The same migration establishes idempotent upload reservations and immutable
object-generation verification. Existing rows remain readable; legacy failed
rows receive safe retry copy so the new UI never strands them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plan_item_assets", sa.Column("error_code", sa.Text(), nullable=True))
    op.add_column("plan_item_assets", sa.Column("error_detail", sa.Text(), nullable=True))
    op.add_column(
        "plan_item_assets",
        sa.Column("error_retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "plan_item_assets",
        sa.Column("analysis_attempt_token", sa.Text(), nullable=True),
    )
    op.add_column(
        "plan_item_assets",
        sa.Column("analysis_attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "plan_item_assets",
        sa.Column("analysis_last_dispatched_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "plan_item_assets",
        sa.Column("analysis_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column("plan_item_assets", sa.Column("client_upload_id", sa.Text(), nullable=True))
    op.add_column("plan_item_assets", sa.Column("upload_content_type", sa.Text(), nullable=True))
    op.add_column(
        "plan_item_assets",
        sa.Column("upload_size_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "plan_item_assets",
        sa.Column("upload_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column("plan_item_assets", sa.Column("gcs_generation", sa.Text(), nullable=True))
    op.add_column("plan_item_assets", sa.Column("correlation_id", sa.Text(), nullable=True))
    op.create_unique_constraint(
        "uq_plan_item_assets_item_client_upload",
        "plan_item_assets",
        ["plan_item_id", "client_upload_id"],
    )
    op.create_index(
        "idx_plan_item_assets_analysis_state",
        "plan_item_assets",
        ["status", "analysis_last_dispatched_at", "analysis_started_at"],
    )
    op.create_index(
        "idx_plan_item_assets_reservation_expiry",
        "plan_item_assets",
        ["status", "upload_expires_at"],
    )
    op.execute(
        sa.text(
            """
            UPDATE plan_item_assets
               SET error_code = COALESCE(error_code, 'analysis_temporarily_unavailable'),
                   error_detail = COALESCE(
                       error_detail,
                       'Kria temporarily could not analyze this file. Try again.'
                   ),
                   error_retryable = true
             WHERE status = 'failed'
            """
        )
    )


def downgrade() -> None:
    op.drop_index("idx_plan_item_assets_reservation_expiry", table_name="plan_item_assets")
    op.drop_index("idx_plan_item_assets_analysis_state", table_name="plan_item_assets")
    op.drop_constraint(
        "uq_plan_item_assets_item_client_upload",
        "plan_item_assets",
        type_="unique",
    )
    op.drop_column("plan_item_assets", "correlation_id")
    op.drop_column("plan_item_assets", "gcs_generation")
    op.drop_column("plan_item_assets", "upload_expires_at")
    op.drop_column("plan_item_assets", "upload_size_bytes")
    op.drop_column("plan_item_assets", "upload_content_type")
    op.drop_column("plan_item_assets", "client_upload_id")
    op.drop_column("plan_item_assets", "analysis_started_at")
    op.drop_column("plan_item_assets", "analysis_last_dispatched_at")
    op.drop_column("plan_item_assets", "analysis_attempt_count")
    op.drop_column("plan_item_assets", "analysis_attempt_token")
    op.drop_column("plan_item_assets", "error_retryable")
    op.drop_column("plan_item_assets", "error_detail")
    op.drop_column("plan_item_assets", "error_code")
