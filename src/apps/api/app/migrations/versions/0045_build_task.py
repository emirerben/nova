"""Create build_task (autonomous dev loop, M4 — builder cron queue).

Revision ID: 0045
Revises: 0044
Create Date: 2026-06-02

The Postgres-backed task queue the GitHub Actions builder cron claims from
(Eng Review D2). Atomic claim is `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`
over `(status, priority, created_at)`; the stale-task reaper sweeps
`in_progress` rows by `(status, claimed_at)`. Both supporting indexes are
created here.

`id` is app-generated (default=uuid.uuid4 on the model). Status + provenance
carry DB CHECK constraints (small, fixed vocabularies — unlike plan_items.
edit_format which is intentionally open). `status`/`attempt_count`/`provenance`/
`priority` get server_defaults so a hand-inserted row (or the admin mint
endpoint) only needs `title`.

Security invariant (CEO D3): provenance distinguishes trusted (rubric-gap
finder / failing evals / founder notes) from untrusted (VideoFeedback notes).
Only trusted signals may mint a task in v1 — enforced in
app.services.build_task_repo, not the DB (the column merely records origin).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "build_task",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("progress_note", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provenance", sa.Text(), nullable=False, server_default="trusted"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("claimed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'in_progress', 'blocked', 'done')",
            name="ck_build_task_status",
        ),
        sa.CheckConstraint(
            "provenance IN ('trusted', 'untrusted')",
            name="ck_build_task_provenance",
        ),
    )
    # Claim path: WHERE status='queued' ORDER BY priority, created_at LIMIT 1.
    op.create_index(
        "idx_build_task_status_priority_created",
        "build_task",
        ["status", "priority", "created_at"],
    )
    # Reaper path: WHERE status='in_progress' AND claimed_at < cutoff.
    op.create_index(
        "idx_build_task_status_claimed",
        "build_task",
        ["status", "claimed_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_build_task_status_claimed", table_name="build_task")
    op.drop_index("idx_build_task_status_priority_created", table_name="build_task")
    op.drop_table("build_task")
