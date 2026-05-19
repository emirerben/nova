"""Add video_templates.recipe_cached_versions for staleness detection.

Revision ID: 0029
Revises: 0028
Create Date: 2026-05-19

Stores the ``{agent_name: prompt_version}`` map captured at the moment
``recipe_cached`` was written. Lets the admin UI flag templates whose
materialized recipe was produced by an older prompt than the live
``AgentSpec.prompt_version`` so they can be reanalyzed explicitly.

NULL on existing rows means "unknown" — the admin UI treats NULL as stale
so the next deploy makes every existing template a reanalyze candidate
without a backfill job. Reanalyzing populates the column.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "video_templates",
        sa.Column(
            "recipe_cached_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("video_templates", "recipe_cached_versions")
