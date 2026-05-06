"""Add music variant columns to video_templates.

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-18

Adds template_type, parent_template_id, and music_track_id to
video_templates for the music-variant parent/child system.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "video_templates",
        sa.Column("template_type", sa.Text(), nullable=False, server_default="standard"),
    )
    op.add_column(
        "video_templates",
        sa.Column("parent_template_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "video_templates",
        sa.Column("music_track_id", sa.Text(), nullable=True),
    )

    # Foreign keys
    op.create_foreign_key(
        "fk_template_parent",
        "video_templates",
        "video_templates",
        ["parent_template_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_template_music_track",
        "video_templates",
        "music_tracks",
        ["music_track_id"],
        ["id"],
    )

    # Check constraint for valid template_type values
    op.create_check_constraint(
        "ck_template_type",
        "video_templates",
        "template_type IN ('standard', 'music_parent', 'music_child')",
    )

    # Unique constraint: one child per parent+track pair
    op.create_unique_constraint(
        "uq_parent_track",
        "video_templates",
        ["parent_template_id", "music_track_id"],
    )

    # Indexes for filtering
    op.create_index("idx_templates_type", "video_templates", ["template_type"])
    op.create_index("idx_templates_parent", "video_templates", ["parent_template_id"])

    # Also update the recipe version trigger check to allow 'remerge'
    op.drop_constraint("ck_recipe_version_trigger", "template_recipe_versions")
    op.create_check_constraint(
        "ck_recipe_version_trigger",
        "template_recipe_versions",
        "trigger IN ('initial_analysis', 'reanalysis', 'manual_edit', 'remerge')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_recipe_version_trigger", "template_recipe_versions")
    op.create_check_constraint(
        "ck_recipe_version_trigger",
        "template_recipe_versions",
        "trigger IN ('initial_analysis', 'reanalysis', 'manual_edit')",
    )

    op.drop_index("idx_templates_parent", "video_templates")
    op.drop_index("idx_templates_type", "video_templates")
    op.drop_constraint("uq_parent_track", "video_templates")
    op.drop_constraint("ck_template_type", "video_templates")
    op.drop_constraint("fk_template_music_track", "video_templates")
    op.drop_constraint("fk_template_parent", "video_templates")
    op.drop_column("video_templates", "music_track_id")
    op.drop_column("video_templates", "parent_template_id")
    op.drop_column("video_templates", "template_type")
