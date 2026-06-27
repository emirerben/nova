"""Add per-item content_mode override to plan_items.

Revision ID: 0058
Revises: 0057
Create Date: 2026-06-27

Changes:
  plan_items.content_mode: new nullable Text column, no server_default.
    NULL  = inherit the content_mode from the owning persona (existing behaviour
            for every legacy row and any item the user has never toggled).
    "create_new"       = "Planning to film" — show shot-plan / ShotSlotUploader flow.
    "existing_footage" = "I already have footage" — skip plan, pool upload only.
    "mixed"            = combination of the two.

  No server_default is intentional: NULL signals "no per-item preference; inherit
  persona". This is additive and non-destructive — all existing rows stay NULL and
  behave exactly as before.

  Only affects the upload UI on the plan-item page. The render archetype is driven
  solely by edit_format + voiceover_gcs_path + filming_guide; content_mode has zero
  effect on job dispatch or output.
"""

import sqlalchemy as sa
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column("content_mode", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # WARNING: rolling back drops any per-item content_mode overrides the user
    # has saved. This is data-safe immediately after deploy (before users interact
    # with the new montage sub-picker), but destructive after real usage. Prefer
    # disabling the UI toggle in app code over running `alembic downgrade`.
    op.drop_column("plan_items", "content_mode")
