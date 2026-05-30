"""Create video_feedback (feedback loop, Phase 2).

Revision ID: 0043
Revises: 0042
Create Date: 2026-05-30

The raw signal store behind the feedback loop: per-video 👍/👎/more-like-this/note
and plan-level "Tell the AI" steer notes. User-scoped writes. A deterministic
rollup compresses these into content_plans.preference_summary (0042).

`id` is app-generated (default=uuid.uuid4 on the model), matching the rest of the
schema. `job_id`/`content_plan_id` are both nullable — exactly one is set per row
(enforced in the write endpoint, not the DB). The one-thumb-per-video rule is also
app-enforced (delete-then-insert) so a `note` row can coexist with a thumb — hence
no partial unique index here.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "video_feedback",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "content_plan_id",
            UUID(as_uuid=True),
            sa.ForeignKey("content_plans.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("signal", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "signal IN ('up', 'down', 'more_like_this', 'note')",
            name="ck_video_feedback_signal",
        ),
    )
    op.create_index("idx_video_feedback_user_created", "video_feedback", ["user_id", "created_at"])
    op.create_index("idx_video_feedback_job", "video_feedback", ["job_id"])
    op.create_index("idx_video_feedback_content_plan", "video_feedback", ["content_plan_id"])


def downgrade() -> None:
    op.drop_index("idx_video_feedback_content_plan", table_name="video_feedback")
    op.drop_index("idx_video_feedback_job", table_name="video_feedback")
    op.drop_index("idx_video_feedback_user_created", table_name="video_feedback")
    op.drop_table("video_feedback")
