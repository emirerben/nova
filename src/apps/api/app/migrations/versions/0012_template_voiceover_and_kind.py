"""Add voiceover_gcs_path column + backfill template_kind discriminator.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-04

Originally numbered 0010, renumbered to 0012 because PR #43 had already
deployed its own 0010 (template_music_variants) and 0011 (music_track_recipe)
to production. Renumbering keeps a single linear alembic head and lets this
migration land on top of prod's existing 0011 state cleanly.

Zero-downtime safe:
- Additive nullable column (voiceover_gcs_path)
- JSONB backfill of template_kind on existing rows is idempotent
  (only sets when missing). Existing templates start working with
  the new "multiple_videos" discriminator immediately.
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "video_templates",
        sa.Column("voiceover_gcs_path", sa.Text(), nullable=True),
    )
    op.execute(
        """
        UPDATE video_templates
        SET recipe_cached = jsonb_set(
            COALESCE(recipe_cached, '{}'::jsonb),
            '{template_kind}',
            '"multiple_videos"'
        )
        WHERE recipe_cached IS NULL
           OR NOT (recipe_cached ? 'template_kind')
        """
    )


def downgrade() -> None:
    # Forward-only data: only strip template_kind from rows that the upgrade
    # backfilled (value == 'multiple_videos'). Rows authored AFTER this
    # migration with template_kind='single_video' (or any other non-default
    # value) keep their discriminator. Otherwise downgrade-then-upgrade would
    # silently rewrite those rows back to 'multiple_videos' and mis-route the
    # job.
    op.execute(
        """
        UPDATE video_templates
        SET recipe_cached = recipe_cached - 'template_kind'
        WHERE recipe_cached ? 'template_kind'
          AND recipe_cached->>'template_kind' = 'multiple_videos'
        """
    )
    op.drop_column("video_templates", "voiceover_gcs_path")
