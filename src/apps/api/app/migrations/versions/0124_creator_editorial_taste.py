"""Owner-confirmed editorial examples, versioned rules and exact-proposal feedback.

Revision ID: 0124
Revises: 0123
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None


def upgrade():
    def owner():
        return sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        )

    def identity():
        return sa.Column("id", pg.UUID(as_uuid=True), primary_key=True)

    def created():
        return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now())

    op.create_table(
        "creator_editorial_taste_examples",
        identity(),
        owner(),
        sa.Column(
            "plan_item_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("content", pg.JSONB(), nullable=False),
        created(),
        sa.UniqueConstraint(
            "user_id", "plan_item_id", "source_sha256", name="uq_editorial_example_source"
        ),
    )
    op.create_index(
        "idx_editorial_example_project", "creator_editorial_taste_examples", ["plan_item_id"]
    )
    op.create_table(
        "creator_editorial_taste_revisions",
        identity(),
        owner(),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        created(),
        sa.UniqueConstraint("user_id", "revision", name="uq_editorial_taste_revision"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_editorial_taste_request"),
        sa.CheckConstraint("revision >= 1", name="ck_editorial_taste_revision"),
        sa.CheckConstraint("octet_length(payload::text) <= 16000", name="ck_editorial_taste_size"),
    )
    op.create_unique_constraint(
        "uq_collection_story_owner", "media_collection_stories", ["id", "user_id"]
    )
    op.create_table(
        "creator_idea_feedback",
        identity(),
        owner(),
        sa.Column("story_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("story_revision", sa.Integer(), nullable=False),
        sa.Column("client_event_id", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("reasons", pg.JSONB(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("rewrite", sa.Text(), nullable=False),
        sa.Column("proposal", pg.JSONB(), nullable=False),
        created(),
        sa.ForeignKeyConstraint(
            ["story_id", "user_id"],
            ["media_collection_stories.id", "media_collection_stories.user_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("user_id", "client_event_id", name="uq_idea_feedback_request"),
        sa.CheckConstraint("story_revision >= 1", name="ck_idea_feedback_revision"),
        sa.CheckConstraint(
            "verdict IN ('would_post','needs_work','not_for_me')", name="ck_idea_feedback_verdict"
        ),
    )
    op.create_index(
        "idx_idea_feedback_owner_created", "creator_idea_feedback", ["user_id", "created_at"]
    )


def downgrade():
    connection = op.get_bind()
    for table in (
        "creator_editorial_taste_examples",
        "creator_editorial_taste_revisions",
        "creator_idea_feedback",
    ):
        if connection.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError(
                "Refusing to discard creator taste; erase it explicitly before downgrade"
            )
    op.drop_table("creator_idea_feedback")
    op.drop_constraint("uq_collection_story_owner", "media_collection_stories", type_="unique")
    op.drop_table("creator_editorial_taste_revisions")
    op.drop_table("creator_editorial_taste_examples")
