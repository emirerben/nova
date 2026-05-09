"""Add video_templates.required_inputs JSONB column.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-09

Holds the per-template list of user inputs the upload UI must collect
(e.g. location). Lives on a dedicated column rather than recipe_cached
because the recipe is overwritten wholesale on every (re-)analysis.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "video_templates",
        sa.Column(
            "required_inputs",
            postgresql.JSONB,
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("video_templates", "required_inputs")
