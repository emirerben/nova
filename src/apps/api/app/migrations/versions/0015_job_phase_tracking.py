"""Add job phase-tracking columns: current_phase, phase_log, started_at, finished_at.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-13

Surfaces pipeline progress to the frontend so users see motion during the 1-5
min template render, instead of an opaque "processing" status for the whole
window. `current_phase` is the live phase name; `phase_log` is the JSONB
append-only history of completed phases with elapsed_ms; `started_at` /
`finished_at` give true wall-time without depending on `updated_at` (which
ticks on every write).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("current_phase", sa.Text(), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "phase_log",
            postgresql.JSONB,
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "jobs",
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "finished_at")
    op.drop_column("jobs", "started_at")
    op.drop_column("jobs", "phase_log")
    op.drop_column("jobs", "current_phase")
