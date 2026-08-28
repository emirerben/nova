"""Index the bounded durable video-poster cleanup sweep.

Revision ID: 0091
Revises: 0090
Create Date: 2026-08-28
"""

from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The jobs table is continuously written by render workers. Build the
    # sparse sweep index without holding a write-blocking table lock for the
    # duration of the scan.
    with op.get_context().autocommit_block():
        # A failed prior CONCURRENTLY attempt can leave an invalid same-name
        # index while Alembic still records 0090. Remove that artifact first;
        # CREATE ... IF NOT EXISTS would otherwise accept a broken index.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_jobs_video_poster_cleanup_sweep")
        op.execute(
            "CREATE INDEX CONCURRENTLY "
            "idx_jobs_video_poster_cleanup_sweep ON jobs (updated_at, id) "
            "WHERE jsonb_typeof(assembly_plan) = 'object' "
            "AND assembly_plan ? '_poster_backfill_cleanup_receipts'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_jobs_video_poster_cleanup_sweep")
