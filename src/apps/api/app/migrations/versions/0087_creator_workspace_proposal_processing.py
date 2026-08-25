"""Add a durable processing claim for workspace relevance proposals."""

import sqlalchemy as sa
from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_creator_workspace_proposals_status",
        "creator_workspace_proposals",
        type_="check",
    )
    op.create_check_constraint(
        "ck_creator_workspace_proposals_status",
        "creator_workspace_proposals",
        "status IN ('pending','processing','ready','failed','approved','rejected')",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE creator_workspace_proposals "
            "SET status = 'failed', error_code = 'processing_status_rollback' "
            "WHERE status = 'processing'"
        )
    )
    op.drop_constraint(
        "ck_creator_workspace_proposals_status",
        "creator_workspace_proposals",
        type_="check",
    )
    op.create_check_constraint(
        "ck_creator_workspace_proposals_status",
        "creator_workspace_proposals",
        "status IN ('pending','ready','failed','approved','rejected')",
    )
