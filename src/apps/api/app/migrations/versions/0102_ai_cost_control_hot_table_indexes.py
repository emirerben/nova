"""Add cost-control indexes and FK on the hot agent_run table safely.

Revision ID: 0102
Revises: 0101
Create Date: 2026-09-08
"""

from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These indexes scan an existing, continuously written table. Building
    # them outside a transaction avoids blocking AgentRun inserts for the
    # duration of the scan. The FK is installed without an initial table scan;
    # VALIDATE uses a weaker lock that permits ordinary reads and writes. Each
    # object is dropped first because this autocommit migration can be retried
    # after a partial deploy; a failed concurrent build may leave an invalid
    # index whose name would otherwise make the retry fail or silently skip it.
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_run_usage_created")
        op.execute(
            "CREATE INDEX CONCURRENTLY idx_agent_run_usage_created "
            "ON agent_run (environment, usage_purpose, created_at)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_run_test_run")
        op.execute(
            "CREATE INDEX CONCURRENTLY idx_agent_run_test_run "
            "ON agent_run (test_run_id, created_at)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_run_cost_reservation")
        op.execute(
            "CREATE INDEX CONCURRENTLY idx_agent_run_cost_reservation "
            "ON agent_run (cost_reservation_id)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_jobs_retention_cursor")
        op.execute("CREATE INDEX CONCURRENTLY idx_jobs_retention_cursor ON jobs (updated_at, id)")
        op.execute("ALTER TABLE agent_run DROP CONSTRAINT IF EXISTS fk_agent_run_cost_reservation")
        op.execute(
            "ALTER TABLE agent_run ADD CONSTRAINT fk_agent_run_cost_reservation "
            "FOREIGN KEY (cost_reservation_id) REFERENCES ai_cost_reservations (id) "
            "ON DELETE SET NULL NOT VALID"
        )
        op.execute("ALTER TABLE agent_run VALIDATE CONSTRAINT fk_agent_run_cost_reservation")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE agent_run DROP CONSTRAINT IF EXISTS fk_agent_run_cost_reservation")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_jobs_retention_cursor")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_run_cost_reservation")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_run_test_run")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_run_usage_created")
