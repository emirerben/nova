"""Add admin lifecycle columns to video_templates + recipe_versions table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-28

Adds nullable columns to video_templates:
  - published_at: when template was made public
  - archived_at: soft-archive timestamp
  - description: admin notes
  - source_url: original TikTok URL for reference
  - thumbnail_gcs_path: auto-extracted frame for gallery display

Creates template_recipe_versions table for tracking analysis history.

Adds index on jobs.template_id for metrics queries.

Backfills published_at = created_at for existing ready templates so the
public listing filter (published_at IS NOT NULL) remains backward-compatible.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── New columns on video_templates ────────────────────────────────────
    op.add_column(
        "video_templates",
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "video_templates",
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column("video_templates", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("video_templates", sa.Column("source_url", sa.Text(), nullable=True))
    op.add_column("video_templates", sa.Column("thumbnail_gcs_path", sa.Text(), nullable=True))

    # Backfill: existing ready templates get published_at = created_at
    op.execute(
        "UPDATE video_templates SET published_at = created_at WHERE analysis_status = 'ready'"
    )

    # ── template_recipe_versions table ────────────────────────────────────
    op.create_table(
        "template_recipe_versions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "template_id",
            sa.Text(),
            sa.ForeignKey("video_templates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("recipe", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "trigger IN ('initial_analysis', 'reanalysis', 'manual_edit')",
            name="ck_recipe_version_trigger",
        ),
    )
    op.create_index(
        "idx_recipe_versions_template_created",
        "template_recipe_versions",
        ["template_id", "created_at"],
    )

    # ── Index on jobs.template_id (if_not_exists for idempotency) ─────────
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_template_id ON jobs (template_id)"
    )


def downgrade() -> None:
    op.drop_index("idx_jobs_template_id", table_name="jobs")
    op.drop_index("idx_recipe_versions_template_created", table_name="template_recipe_versions")
    op.drop_table("template_recipe_versions")
    op.drop_column("video_templates", "thumbnail_gcs_path")
    op.drop_column("video_templates", "source_url")
    op.drop_column("video_templates", "description")
    op.drop_column("video_templates", "archived_at")
    op.drop_column("video_templates", "published_at")
