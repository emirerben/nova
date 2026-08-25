"""Bound workspace lookups and add child-side foreign-key indexes."""

import sqlalchemy as sa
from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_creator_agent_sessions_complete_latest",
        "creator_agent_sessions",
        ["creator_id", "plan_item_id", "updated_at", "created_at", "id"],
        postgresql_where=sa.text(
            "target_job_id IS NOT NULL AND target_variant_id IS NOT NULL "
            "AND target_generation_id IS NOT NULL"
        ),
    )
    op.create_index(
        "idx_creator_workspace_proposals_target_item",
        "creator_workspace_proposals",
        ["target_plan_item_id"],
    )
    op.create_index(
        "idx_creator_workspace_proposals_result_item",
        "creator_workspace_proposals",
        ["result_plan_item_id"],
    )
    op.create_index(
        "idx_creator_workspace_deliverables_creator",
        "creator_workspace_deliverables",
        ["creator_id"],
    )
    op.create_index(
        "idx_creator_workspace_deliverables_plan",
        "creator_workspace_deliverables",
        ["plan_id"],
    )
    op.create_index(
        "idx_creator_workspace_deliverables_session",
        "creator_workspace_deliverables",
        ["creator_session_id"],
    )
    op.create_index(
        "idx_creator_workspace_deliverables_job",
        "creator_workspace_deliverables",
        ["job_id"],
    )
    op.create_index(
        "idx_creator_workspace_pref_receipt",
        "creator_workspace_preference_signals",
        ["receipt_id"],
    )


def downgrade() -> None:
    for index_name, table_name in (
        ("idx_creator_workspace_pref_receipt", "creator_workspace_preference_signals"),
        ("idx_creator_workspace_deliverables_job", "creator_workspace_deliverables"),
        ("idx_creator_workspace_deliverables_session", "creator_workspace_deliverables"),
        ("idx_creator_workspace_deliverables_plan", "creator_workspace_deliverables"),
        ("idx_creator_workspace_deliverables_creator", "creator_workspace_deliverables"),
        ("idx_creator_workspace_proposals_result_item", "creator_workspace_proposals"),
        ("idx_creator_workspace_proposals_target_item", "creator_workspace_proposals"),
        ("idx_creator_agent_sessions_complete_latest", "creator_agent_sessions"),
    ):
        op.drop_index(index_name, table_name=table_name)
