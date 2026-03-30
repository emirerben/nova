"""Seed the synthetic MVP user row required by all job FK references.

Revision ID: 0007
Revises: 0006
Create Date: 2026-03-30

Context: All routes (presigned, template_jobs, uploads) use the hard-coded
SYNTHETIC_USER_ID = '00000000-0000-0000-0000-000000000001' as a placeholder
until real auth is added. The jobs table has a NOT-NULL FK to users.id, so
the synthetic user must exist in production before any job can be created.

Local dev had this row inserted manually / via tests. Production was missing
it, causing every POST /template-jobs (and any other job-creating endpoint)
to fail with a ForeignKeyViolationError → HTTP 500.

This migration inserts the row idempotently (ON CONFLICT DO NOTHING) so it
is safe to run even if the row already exists in dev / staging.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

SYNTHETIC_USER_ID = "00000000-0000-0000-0000-000000000001"
SYNTHETIC_USER_EMAIL = "synthetic-mvp@nova.internal"


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO users (id, email, name)
        VALUES (
            '{SYNTHETIC_USER_ID}',
            '{SYNTHETIC_USER_EMAIL}',
            'Synthetic MVP User'
        )
        ON CONFLICT (id) DO NOTHING;
        """
    )


def downgrade() -> None:
    # Only remove if no jobs reference it — skip silently if referenced
    op.execute(
        f"""
        DELETE FROM users WHERE id = '{SYNTHETIC_USER_ID}'
          AND NOT EXISTS (
            SELECT 1 FROM jobs WHERE user_id = '{SYNTHETIC_USER_ID}'
          );
        """
    )
