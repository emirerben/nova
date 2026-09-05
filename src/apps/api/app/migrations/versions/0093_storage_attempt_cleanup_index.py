"""Index the bounded storage-attempt cleanup sweep.

Revision ID: 0093
Revises: 0092
Create Date: 2026-09-01
"""

from alembic import op

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Render workers update jobs continuously. Build the sparse debt index
    # without taking a write-blocking table lock for the duration of the scan.
    with op.get_context().autocommit_block():
        # A failed concurrent build can leave an invalid same-name index.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_jobs_storage_attempt_cleanup_sweep")
        op.execute(
            "CREATE INDEX CONCURRENTLY "
            "idx_jobs_storage_attempt_cleanup_sweep ON jobs (updated_at, id) "
            "WHERE jsonb_typeof(assembly_plan -> '_speech_cleanup_internal') = 'object' "
            "AND ((assembly_plan -> '_speech_cleanup_internal') "
            "? 'durable_source_copy_pending' "
            "OR (assembly_plan -> '_speech_cleanup_internal') "
            "? 'render_generation_cleanup_pending')"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_jobs_storage_attempt_cleanup_sweep")
