"""Index the circular plan-item job pointer for terminal job deletion."""

from alembic import op

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_plan_items_current_job_id",
        "plan_items",
        ["current_job_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_plan_items_current_job_id", table_name="plan_items")
