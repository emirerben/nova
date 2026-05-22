"""Add created_at indexes for admin list endpoints.

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-22

The admin list endpoints (/admin/jobs, /admin/templates, /admin/music-tracks)
all order by created_at DESC with no supporting index, forcing a full-table
sort on every request. As job volume grew, admin pages slowed visibly.

We add a plain btree on created_at for each of the three tables. A btree
covers DESC scans (PostgreSQL reverses the scan direction), so no DESC
variant is needed. For VideoTemplate we also add a compound
(template_type, created_at) index because /admin/templates filters on
template_type != 'music_child' before ordering — without the compound,
the planner still has to filter post-sort or sort post-filter.

CONCURRENTLY: built outside the migration transaction so the index build
doesn't take an ACCESS EXCLUSIVE lock on the table. If the build fails
mid-flight, the partial index is left as INVALID — drop it and re-run.
"""

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # autocommit_block exits the implicit transaction so CONCURRENTLY is legal.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_jobs_created_at ON jobs (created_at)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_templates_created_at "
            "ON video_templates (created_at)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_templates_type_created "
            "ON video_templates (template_type, created_at)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_music_tracks_created_at "
            "ON music_tracks (created_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_music_tracks_created_at")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_templates_type_created")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_templates_created_at")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_jobs_created_at")
