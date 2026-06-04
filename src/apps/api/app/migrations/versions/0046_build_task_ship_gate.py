"""Extend build_task for the dev-loop ship-gate (Phase 2).

Revision ID: 0046
Revises: 0045
Create Date: 2026-06-04

Phase 2 of the autonomous dev-loop inserts a quality-gate + PR step between the
builder's WIP branch and a human merge. The builder no longer ends at `done`; it
routes built work through `gating` (a separate gate tick runs the hard gates and
rebases the branch onto origin/main) and parks a gated, PR-open task in
`awaiting_approval` for the founder to merge by hand. Phase 3's phone surface
reads the same `awaiting_approval` rows — no rename later.

Adds the two non-terminal statuses + the columns the gate/PR step needs:
  head_sha          — the exact pushed commit the gate must match before it runs
                      (guards against gating a branch the builder never finished
                      pushing).
  pr_url / pr_number— the opened PR (NULL until open_pr).
  gate_report       — JSONB record of each gate's pass/fail + the advisory
                      /qa + codex results, surfaced in the PR body and digest.

Postgres can't ALTER a CHECK in place, so the status check is dropped and
recreated with the expanded vocabulary. Keep this in lockstep with
BuildTask.STATUSES and the model __table_args__ CheckConstraint — drift between
migration and model breaks create_all() in tests (learning
sqlalchemy-check-constraint-model-migration-drift).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

_STATUS_OLD = "status IN ('queued', 'in_progress', 'blocked', 'done')"
_STATUS_NEW = (
    "status IN ('queued', 'in_progress', 'gating', 'awaiting_approval', "
    "'blocked', 'done')"
)


def upgrade() -> None:
    op.add_column("build_task", sa.Column("head_sha", sa.Text(), nullable=True))
    op.add_column("build_task", sa.Column("pr_url", sa.Text(), nullable=True))
    op.add_column("build_task", sa.Column("pr_number", sa.Integer(), nullable=True))
    op.add_column("build_task", sa.Column("gate_report", JSONB(), nullable=True))
    op.drop_constraint("ck_build_task_status", "build_task", type_="check")
    op.create_check_constraint("ck_build_task_status", "build_task", _STATUS_NEW)


def downgrade() -> None:
    # Revert to the M4 status vocabulary. A row still in gating/awaiting_approval
    # must be migrated out first or the recreate fails — intentional (never
    # silently drop an in-flight gated task).
    op.drop_constraint("ck_build_task_status", "build_task", type_="check")
    op.create_check_constraint("ck_build_task_status", "build_task", _STATUS_OLD)
    op.drop_column("build_task", "gate_report")
    op.drop_column("build_task", "pr_number")
    op.drop_column("build_task", "pr_url")
    op.drop_column("build_task", "head_sha")
