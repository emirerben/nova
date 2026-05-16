"""Add build_started_at column to template_recipe_versions.

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-16

NOTE: originally written as revision 0022 but collided with PR #182's
``0022_agent_runs_and_pipeline_trace`` which merged first while this PR
was in review. Renumbered to 0023 to chain after the agent-runs table
and break the alembic ``Multiple head revisions are present`` deploy
failure — same fix pattern as the documented PR #163/166 / 0020
collision in 0021's docstring. Content is unchanged from the
originally-proposed 0022 version. Caught by adversarial review on PR #181.

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

Semantic note: `build_started_at` is captured at WORKER pickup, not at
button-click time. Celery queue-wait is excluded. For our agentic build
pipeline (where Gemini calls dominate the work) this is the right baseline
for measuring "compute time"; if you ever want end-to-end user-perceived
latency, add a separate `enqueued_at` field set by the route handler.

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

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "template_recipe_versions",
        sa.Column("build_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("template_recipe_versions", "build_started_at")
