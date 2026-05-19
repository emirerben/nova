"""Add single_pass_enabled flag to video_templates.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-15

Per-template allow-list for the single-pass encode rollout. The env-level
``settings.single_pass_encode_enabled`` is a global kill switch; this
column gates which templates actually take the single-pass path when the
env flag is on.

Rollout pattern: flip the env flag to True (no-op while every row is
False), then UPDATE single_pass_enabled=true per template after its
parity + benchmark gate clears. The two-flag AND-gate means flipping
either one alone has zero render impact.

Default false; partial index supports the admin query "show templates
already on single-pass" without bloating writes on the common false case.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "video_templates",
        sa.Column(
            "single_pass_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "idx_video_templates_single_pass",
        "video_templates",
        ["single_pass_enabled"],
        postgresql_where=sa.text("single_pass_enabled = true"),
    )


def downgrade() -> None:
    op.drop_index("idx_video_templates_single_pass", table_name="video_templates")
    op.drop_column("video_templates", "single_pass_enabled")
