"""Add durable plan-item ownership for pre-render agent traces.

Revision ID: 0107
Revises: 0106

Proposal planning runs agents before the render Job is minted.  The nullable
owner preserves those traces without a fake jobs.id and keeps all legacy owner
shapes valid.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_run",
        sa.Column(
            "plan_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_agent_run_plan_item_id_created",
        "agent_run",
        ["plan_item_id", "created_at"],
    )
    op.drop_constraint("ck_agent_run_has_owner", "agent_run", type_="check")
    op.create_check_constraint(
        "ck_agent_run_has_owner",
        "agent_run",
        "(job_id IS NOT NULL) "
        "OR (template_id IS NOT NULL) "
        "OR (music_track_id IS NOT NULL) "
        "OR (creator_agent_session_id IS NOT NULL) "
        "OR (plan_item_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_run_has_owner", "agent_run", type_="check")
    # Rows exclusively owned by a proposal cannot satisfy the older invariant.
    # Deleting only those feature-owned rows retains all pre-existing owners.
    op.execute(
        "DELETE FROM agent_run WHERE plan_item_id IS NOT NULL "
        "AND job_id IS NULL AND template_id IS NULL AND music_track_id IS NULL "
        "AND creator_agent_session_id IS NULL"
    )
    op.drop_index("idx_agent_run_plan_item_id_created", table_name="agent_run")
    op.drop_column("agent_run", "plan_item_id")
    op.create_check_constraint(
        "ck_agent_run_has_owner",
        "agent_run",
        "(job_id IS NOT NULL) "
        "OR (template_id IS NOT NULL) "
        "OR (music_track_id IS NOT NULL) "
        "OR (creator_agent_session_id IS NOT NULL)",
    )
