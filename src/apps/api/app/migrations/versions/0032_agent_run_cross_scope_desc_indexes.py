"""Add DESC partial indexes for agent_run context lookups.

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-22

The admin job debug page fetches the most recent agent_run rows for a linked
template or music track. Popular templates can have hundreds of runs, so these
lookups need indexes whose leading column matches the filter and whose ordering
matches the newest-first LIMIT query.
"""

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # autocommit_block exits the implicit transaction so CONCURRENTLY is legal.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "idx_agent_run_template_id_created_desc "
            "ON agent_run (template_id, created_at DESC) "
            "WHERE template_id IS NOT NULL"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "idx_agent_run_music_track_id_created_desc "
            "ON agent_run (music_track_id, created_at DESC) "
            "WHERE music_track_id IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_run_music_track_id_created_desc")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_run_template_id_created_desc")
