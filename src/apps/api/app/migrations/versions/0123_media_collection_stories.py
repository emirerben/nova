"""Durable camera-roll story candidates and correction/outcome references."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "media_collection_stories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("collection_revision", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("story_json", postgresql.JSONB(), nullable=False),
        sa.Column("moments_json", postgresql.JSONB(), nullable=False),
        sa.Column("events_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["collection_id", "user_id"],
            ["media_collections.id", "media_collections.user_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND collection_revision >= 1", name="ck_collection_story_revision"
        ),
    )
    op.create_index(
        "idx_collection_stories_owner",
        "media_collection_stories",
        ["user_id", "collection_id", "created_at"],
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM media_collection_stories)")
    ).scalar():
        raise RuntimeError("Refusing to discard reviewed stories; archive them before downgrade")
    op.drop_table("media_collection_stories")
