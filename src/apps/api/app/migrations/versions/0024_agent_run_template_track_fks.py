"""Add agent_run.template_id and agent_run.music_track_id.

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-17

PR #182 wired ``persist_agent_run`` so every agent invocation lands a row in
``agent_run``. The persistence layer drops rows whose ``RunContext.job_id`` is
not a UUID, which hides every template-analysis and track-analysis run from
the admin debug view (those call sites pass ``"template:<uuid>"`` /
``"track:<uuid>"``). This migration adds two nullable FKs so those runs can
land keyed on their owning entity instead of an in-flight job, and a check
constraint that guarantees every row points at *something*.

Existing rows are unaffected: every row in ``agent_run`` today has a non-null
``job_id`` (the prefix-jobs were dropped before insert), so the new check
constraint is satisfied retroactively.
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # video_templates.id and music_tracks.id are Text in this schema
    # (see models.py:VideoTemplate/MusicTrack). The FK column type must
    # match the target's type — Postgres rejects uuid→text FKs with
    # "incompatible types". Use Text here for the same reason Job.template_id
    # / Job.music_track_id are Text.
    op.add_column(
        "agent_run",
        sa.Column("template_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "agent_run",
        sa.Column("music_track_id", sa.Text(), nullable=True),
    )
    # ondelete=CASCADE mirrors the existing job_id FK on this table. We
    # CANNOT use SET NULL: combined with ck_agent_run_has_owner below, a
    # SET NULL cascade on the only non-null FK would produce a row with
    # all-null owners and abort the parent DELETE on a check-constraint
    # violation. CASCADE preserves the invariant.
    op.create_foreign_key(
        "fk_agent_run_template_id",
        "agent_run",
        "video_templates",
        ["template_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_agent_run_music_track_id",
        "agent_run",
        "music_tracks",
        ["music_track_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "idx_agent_run_template_id_created",
        "agent_run",
        ["template_id", "created_at"],
    )
    op.create_index(
        "idx_agent_run_music_track_id_created",
        "agent_run",
        ["music_track_id", "created_at"],
    )
    op.create_check_constraint(
        "ck_agent_run_has_owner",
        "agent_run",
        "(job_id IS NOT NULL) "
        "OR (template_id IS NOT NULL) "
        "OR (music_track_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_run_has_owner", "agent_run", type_="check")
    op.drop_index("idx_agent_run_music_track_id_created", table_name="agent_run")
    op.drop_index("idx_agent_run_template_id_created", table_name="agent_run")
    op.drop_constraint("fk_agent_run_music_track_id", "agent_run", type_="foreignkey")
    op.drop_constraint("fk_agent_run_template_id", "agent_run", type_="foreignkey")
    op.drop_column("agent_run", "music_track_id")
    op.drop_column("agent_run", "template_id")
