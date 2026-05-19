"""Add celery_task_id to jobs.

Revision ID: 0027
Revises: 0026
Create Date: 2026-05-19

The admin job-debug UI needs a path from a Job row back to the live Celery
task so it can show worker assignment, "is this task still active?", and
support cancellation via celery_app.control.revoke(). Pre-migration, only
drive_import set task_id=job_id on apply_async — template/music/auto-music
orchestrators let Celery auto-generate UUIDs, so there was no DB → Celery
mapping at all.

Going forward, every orchestrator dispatch site routes through
app.services.job_dispatch.enqueue_orchestrator(), which writes
celery_task_id=str(job_id) onto the row and passes task_id=str(job_id) to
apply_async. Storing the value explicitly (vs. inferring it from id) keeps
the convention discoverable at admin-endpoint code sites and leaves room
for a future per-attempt task_id without a schema change.

Legacy rows stay NULL. The reaper continues to handle their cleanup the
old way (inspect args[0] across active+reserved tasks).

No index — lookups are by job_id (primary key), never by celery_task_id.
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("celery_task_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "celery_task_id")
