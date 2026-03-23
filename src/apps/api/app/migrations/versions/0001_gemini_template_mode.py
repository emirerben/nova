"""Add video_templates table and template fields to jobs.

Revision ID: 0001
Revises:
Create Date: 2026-03-23

Zero-downtime safe — additive only (new table + new nullable columns with defaults).
Deployment order: migrate → deploy → register template via POST /admin/templates.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── New table: video_templates ──────────────────────────────────────────
    op.create_table(
        "video_templates",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("gcs_path", sa.Text, nullable=False),
        sa.Column("recipe_cached", JSONB, nullable=True),
        sa.Column("recipe_cached_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("analysis_status", sa.Text, nullable=False, server_default="analyzing"),
        sa.Column("required_clips_min", sa.Integer, nullable=False, server_default="5"),
        sa.Column("required_clips_max", sa.Integer, nullable=False, server_default="10"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ── Extend jobs table ───────────────────────────────────────────────────
    # All three columns are additive with safe defaults — zero-downtime.
    op.add_column(
        "jobs",
        sa.Column("job_type", sa.Text, nullable=False, server_default="default"),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "template_id",
            sa.Text,
            sa.ForeignKey("video_templates.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "jobs",
        sa.Column("assembly_plan", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "assembly_plan")
    op.drop_column("jobs", "template_id")
    op.drop_column("jobs", "job_type")
    op.drop_table("video_templates")
