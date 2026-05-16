"""Add build_started_at column to template_recipe_versions.

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-16

Adds a nullable `build_started_at TIMESTAMPTZ` column to
`template_recipe_versions`. Paired with the existing `created_at` (which
records end-of-build), this gives us per-version wall-clock without
relying on Langfuse trace aggregation. The Langfuse audit script (PR #179)
covers the backward-looking audit; this column gives us a forward baseline
queryable from any psql session.

Why on `template_recipe_versions` and not `video_templates`:
  - `VideoTemplate.recipe_cached_at` is OVERWRITTEN on every reanalyze
    (the row is shared across versions). A timing column there would only
    ever capture the latest run's start, losing all historical wall-clocks.
  - `TemplateRecipeVersion` is append-only — one row per analyze/reanalyze.
    A column here gives per-run timing forever.

The column is nullable because (a) every existing row has no start
timestamp to backfill with, and (b) the orchestrator might fail before
writing it. Callers must tolerate NULL when querying duration:
  `EXTRACT(EPOCH FROM (created_at - build_started_at))` returns NULL for
  pre-migration rows. Aggregations should `WHERE build_started_at IS NOT NULL`.

Downgrade drops the column — no data preservation needed because the field
is purely observational.
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "template_recipe_versions",
        sa.Column("build_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("template_recipe_versions", "build_started_at")
