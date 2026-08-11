"""Add durable ownership fencing to content plans.

Revision ID: 0071
Revises: 0070
Create Date: 2026-08-11

This release is deliberately additive so the previous application image can
continue running while the fail-closed ownership guards roll out.  Once an
epoch or quarantine has been used, downgrade is unsafe because it would erase
the durable stale-worker fence.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("content_plan_ownership_epoch", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "content_plans",
        sa.Column(
            "ownership_epoch",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "content_plans",
        sa.Column("ownership_quarantined_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Serialize the precondition with every writer before deciding whether the
    # durable fence columns are still unused.  An AccessShare lock from the
    # SELECT alone would allow a concurrent quarantine/epoch update to land
    # between this check and the ALTER TABLE, silently erasing security state.
    op.get_bind().execute(sa.text("LOCK TABLE content_plans IN ACCESS EXCLUSIVE MODE"))
    op.get_bind().execute(sa.text("LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE"))
    used_fences = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT "
                "(SELECT count(*) FROM content_plans "
                " WHERE ownership_epoch <> 0 OR ownership_quarantined_at IS NOT NULL) + "
                "(SELECT count(*) FROM jobs "
                " WHERE content_plan_ownership_epoch IS NOT NULL)"
            )
        )
        .scalar_one()
    )
    if used_fences:
        raise RuntimeError(
            "Cannot downgrade 0071 after a content-plan ownership fence has been used"
        )

    op.drop_column("content_plans", "ownership_quarantined_at")
    op.drop_column("content_plans", "ownership_epoch")
    op.drop_column("jobs", "content_plan_ownership_epoch")
