"""Add nullable auto-music columns to job_clips + jobs.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-15

NOTE: originally written as revision 0020 (PR #163) but collided with
PR #166's ``0020_music_track_best_sections``. PR #166 merged first and
its 0020 was applied to prod; this migration is renumbered to 0021 to
break the alembic ``Multiple head revisions are present`` deploy
failure. Content is unchanged from the original PR #163 version.

Phase 3 of the auto-music feature (see plans/our-current-agentic-template-
scalable-gem.md). Adds the minimum schema needed for the new
``orchestrate_auto_music_job`` Celery task to land variant rows alongside
existing template-mode and music-mode rows:

  - ``job_clips.music_track_id`` (nullable FK → music_tracks.id): which
    track this variant was rendered from. NULL for template-mode and
    legacy music-mode JobClip rows.
  - ``job_clips.match_score`` (nullable float): the matcher's 0-10 score
    for this track on this clip-set. NULL for non-auto-music rows.
  - ``job_clips.match_rationale`` (nullable text): the matcher's editor's-
    voice rationale string, surfaced to users on the variant tile.
  - ``jobs.mode`` (nullable text, defaults NULL): values are ``'auto_music'``
    for new auto-music jobs. NULL for every existing row — the orchestrator
    routes off ``job_type`` (set to ``'auto_music'`` for new jobs) and old
    rows keep their existing ``job_type``-based routing untouched. The
    column exists for analytics / debugging breakdowns and for a future
    explicit ``mode``-based router.

All columns are nullable, no defaults beyond NULL. Existing rows continue
to satisfy the schema as-is. Downgrade drops all four columns — no data
preservation needed because every row a downgrade would touch was created
by Phase 3 code that won't ship before this migration runs.

Indexes: ``idx_job_clips_music_track_id`` on the new FK so the future
admin "where was this track used" query is cheap. No index on ``jobs.mode``
yet — adds it when we have real cardinality.
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_clips",
        sa.Column("music_track_id", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_job_clips_music_track_id",
        "job_clips",
        "music_tracks",
        ["music_track_id"],
        ["id"],
    )
    op.create_index(
        "idx_job_clips_music_track_id",
        "job_clips",
        ["music_track_id"],
    )
    op.add_column(
        "job_clips",
        sa.Column("match_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "job_clips",
        sa.Column("match_rationale", sa.Text(), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("mode", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "mode")
    op.drop_column("job_clips", "match_rationale")
    op.drop_column("job_clips", "match_score")
    op.drop_index("idx_job_clips_music_track_id", table_name="job_clips")
    op.drop_constraint("fk_job_clips_music_track_id", "job_clips", type_="foreignkey")
    op.drop_column("job_clips", "music_track_id")
