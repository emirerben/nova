"""Let agent_run keep runs that have no owner row (KRI-1395).

Revision ID: 0139
Revises: 0138

``agent_run`` required one of five owner foreign keys, and ``persist_agent_run`` dropped every
run that had none (eval harness, background calls, a ``RunContext`` with no job), so the fleet
report could not see them. ``owner_label`` keeps the raw ``RunContext.job_id`` (or "unowned")
for those runs, and the check now accepts it as the owner. ``idx_agent_run_created_at`` serves
the report's window scan, which no owner-leading index can; ``idx_ai_cost_reservations_created_at``
does the same for the ledger cross-check.
"""

import sqlalchemy as sa
from alembic import op

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None

_OWNED = (
    "(job_id IS NOT NULL) "
    "OR (template_id IS NOT NULL) "
    "OR (music_track_id IS NOT NULL) "
    "OR (creator_agent_session_id IS NOT NULL) "
    "OR (plan_item_id IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column("agent_run", sa.Column("owner_label", sa.Text(), nullable=True))
    op.drop_constraint("ck_agent_run_has_owner", "agent_run", type_="check")
    op.create_check_constraint(
        "ck_agent_run_has_owner", "agent_run", f"{_OWNED} OR (owner_label IS NOT NULL)"
    )
    # A scan of the continuously written table: build outside a transaction, and drop first so
    # a retry after a partial deploy cannot leave an invalid index under the same name.
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_run_created_at")
        op.execute("CREATE INDEX CONCURRENTLY idx_agent_run_created_at ON agent_run (created_at)")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_ai_cost_reservations_created_at")
        op.execute(
            "CREATE INDEX CONCURRENTLY idx_ai_cost_reservations_created_at "
            "ON ai_cost_reservations (created_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_ai_cost_reservations_created_at")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_run_created_at")
    op.drop_constraint("ck_agent_run_has_owner", "agent_run", type_="check")
    # Unowned runs cannot satisfy the stricter invariant.
    op.execute("DELETE FROM agent_run WHERE owner_label IS NOT NULL AND NOT (" + _OWNED + ")")
    op.drop_column("agent_run", "owner_label")
    op.create_check_constraint("ck_agent_run_has_owner", "agent_run", _OWNED)
