"""Add is_agentic flag to video_templates.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-14

Partitions templates into manual (legacy, default) vs. agentic (recipe is
generated end-to-end by the agent stack, never hand-edited). Default false so
every existing row stays on the manual path. Partial index supports the
admin filter "show only agentic templates" without bloating writes on the
common false case.
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "video_templates",
        sa.Column(
            "is_agentic",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "idx_video_templates_agentic",
        "video_templates",
        ["is_agentic"],
        postgresql_where=sa.text("is_agentic = true"),
    )


def downgrade() -> None:
    op.drop_index("idx_video_templates_agentic", table_name="video_templates")
    op.drop_column("video_templates", "is_agentic")
