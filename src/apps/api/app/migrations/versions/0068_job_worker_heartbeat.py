"""jobs.worker_heartbeat_at — render-liveness beacon (2026-07-21 OOM incident).

A worker OOM-killed mid-reframe (job e8173a25) leaves its job at
status="rendering" with zero signal for the full acks_late redelivery window
(visibility_timeout=1900s → the user stared at healthy-looking progress for
30+ minutes). The orchestrator now ticks this column every ~30s from a
daemon thread (services/job_phases.job_heartbeat); the generative status
route compares it against now() and reports `retrying: true` once it goes
stale, flipping back automatically when the redelivered attempt resumes
beating.

Additive nullable column — NULL on every legacy row and on jobs from
orchestrators that don't heartbeat (the route treats NULL as "no signal",
never as stale).

Revision ID: 0068
Revises: 0067
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("worker_heartbeat_at", TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "worker_heartbeat_at")
