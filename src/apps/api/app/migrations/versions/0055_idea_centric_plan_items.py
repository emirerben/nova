"""Idea-centric plan: add position, scheduled_date, notes, scenes; nullable day_index/theme.

Revision ID: 0055
Revises: 0054
Create Date: 2026-06-17

Changes:
  plan_items.day_index:    NOT NULL -> nullable (calendar slot, optional going forward)
  plan_items.theme:        NOT NULL -> nullable (bare idea has no theme until AI fills it)
  plan_items.position:     new Integer NOT NULL (user ordering; backfilled from day_index)
  plan_items.scheduled_date: new Date nullable (optional per-idea date pin)
  plan_items.notes:        new Text nullable (freeform notes)
  plan_items.scenes:       new JSONB NOT NULL default '[]' (list of {id, text, transition_after?})

Backfill: position = day_index for all existing rows (backfill runs BEFORE NOT NULL).
Order guarantee: existing plans render identically under order_by="position".
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add position as nullable first (server_default=0 would collapse ordering, so
    #    we use NULL + manual backfill + NOT NULL in that order).
    op.add_column(
        "plan_items",
        sa.Column("position", sa.Integer(), nullable=True),
    )

    # 2. Backfill position from day_index for all existing rows BEFORE setting NOT NULL.
    op.execute("UPDATE plan_items SET position = day_index")

    # 3. Now enforce NOT NULL (all rows have a value from the backfill).
    op.alter_column("plan_items", "position", nullable=False)

    # 4. Make day_index and theme nullable (they are optional in the idea-centric model).
    op.alter_column("plan_items", "day_index", nullable=True)
    op.alter_column("plan_items", "theme", nullable=True)

    # 5. Add new columns.
    op.add_column(
        "plan_items",
        sa.Column("scheduled_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "plan_items",
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "plan_items",
        sa.Column(
            "scenes",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )

    # 6. Add position index (keep the legacy _day index during transition).
    op.create_index(
        "idx_plan_items_content_plan_id_position",
        "plan_items",
        ["content_plan_id", "position"],
    )


def downgrade() -> None:
    op.drop_index("idx_plan_items_content_plan_id_position", table_name="plan_items")
    op.drop_column("plan_items", "scenes")
    op.drop_column("plan_items", "notes")
    op.drop_column("plan_items", "scheduled_date")
    # Re-apply NOT NULL on day_index and theme (only safe if no NULL values exist).
    op.alter_column("plan_items", "theme", nullable=False)
    op.alter_column("plan_items", "day_index", nullable=False)
    op.drop_column("plan_items", "position")
