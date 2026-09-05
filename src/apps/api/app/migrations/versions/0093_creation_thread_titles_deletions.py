"""Persist creation-project titles and deletion idempotency tombstones."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_storage_deletions",
        sa.Column(
            "object_prefixes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "creation_threads",
        sa.Column("title", sa.Text(), server_default="Untitled video", nullable=False),
    )
    op.execute(
        """
        UPDATE creation_threads
        SET title = LEFT(
            COALESCE(NULLIF(BTRIM(state ->> 'title'), ''),
                     NULLIF(BTRIM(state ->> 'intent'), ''),
                     'Untitled video'),
            120)
        WHERE title = 'Untitled video'
        """
    )
    op.create_check_constraint(
        "ck_creation_threads_title_length",
        "creation_threads",
        "length(title) BETWEEN 1 AND 120",
    )
    op.create_table(
        "creation_thread_deletions",
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_creation_thread_deletions_creator",
        "creation_thread_deletions",
        ["creator_id"],
    )
    op.create_table(
        "creation_thread_upload_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creation_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("media_id", sa.Text(), nullable=False),
        sa.Column("object_path", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("thread_id", "media_id", name="uq_creation_thread_upload_media"),
    )
    op.create_index(
        "idx_creation_thread_upload_expiry",
        "creation_thread_upload_reservations",
        ["thread_id", "expires_at"],
    )


def downgrade() -> None:
    # Every 0093 table/column contains durable user state. Serialize the check
    # with all writers and refuse destructive rollback once the migration has
    # been used, matching the guard on the 0092 creation-thread foundation.
    op.execute(
        "LOCK TABLE creation_threads, creation_thread_deletions, "
        "creation_thread_upload_reservations, job_storage_deletions "
        "IN ACCESS EXCLUSIVE MODE"
    )
    bind = op.get_bind()
    thread_count = bind.scalar(sa.text("SELECT count(*) FROM creation_threads"))
    deletion_count = bind.scalar(sa.text("SELECT count(*) FROM creation_thread_deletions"))
    reservation_count = bind.scalar(
        sa.text("SELECT count(*) FROM creation_thread_upload_reservations")
    )
    prefix_count = bind.scalar(
        sa.text("SELECT count(*) FROM job_storage_deletions WHERE object_prefixes <> '[]'::jsonb")
    )
    if thread_count or deletion_count or reservation_count or prefix_count:
        raise RuntimeError(
            "Refusing to downgrade 0093 while project lifecycle data exists; "
            "export and empty creation threads, deletion tombstones, upload "
            "reservations, and prefix cleanup records first."
        )

    op.drop_index(
        "idx_creation_thread_upload_expiry",
        table_name="creation_thread_upload_reservations",
    )
    op.drop_table("creation_thread_upload_reservations")
    op.drop_index("idx_creation_thread_deletions_creator", table_name="creation_thread_deletions")
    op.drop_table("creation_thread_deletions")
    op.drop_constraint("ck_creation_threads_title_length", "creation_threads", type_="check")
    op.drop_column("creation_threads", "title")
    op.drop_column("job_storage_deletions", "object_prefixes")
