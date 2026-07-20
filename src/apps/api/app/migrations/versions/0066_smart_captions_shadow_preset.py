"""Add optional Smart Captions shadow preset assignment.

Revision ID: 0066
Revises: 0065
Create Date: 2026-07-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "creator_style_assignments",
        sa.Column("shadow_preset_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "creator_style_assignments",
        sa.Column("shadow_preset_version", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_creator_style_shadow_pair",
        "creator_style_assignments",
        "(shadow_preset_id IS NULL) = (shadow_preset_version IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_creator_style_shadow_pair",
        "creator_style_assignments",
        type_="check",
    )
    op.drop_column("creator_style_assignments", "shadow_preset_version")
    op.drop_column("creator_style_assignments", "shadow_preset_id")
