"""Add durable chat-first creation threads and transcript events.

The legacy plan rows are left untouched.  Only unscheduled, explicitly edited
drafts are projected into a thread so existing work remains discoverable while
completed jobs continue to be served by the Gallery/job APIs.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "creation_threads",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "state", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "content_plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_plans.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "active_plan_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "active_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "active_creator_agent_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creator_agent_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active','archived','failed')", name="ck_creation_threads_status"
        ),
        sa.CheckConstraint("revision >= 0", name="ck_creation_threads_revision"),
        sa.UniqueConstraint("active_plan_item_id", name="uq_creation_threads_active_plan_item"),
    )
    op.create_index(
        "idx_creation_threads_creator_updated", "creation_threads", ["creator_id", "updated_at"]
    )
    op.create_index("idx_creation_threads_active_job", "creation_threads", ["active_job_id"])
    op.create_index(
        "idx_creation_threads_active_session",
        "creation_threads",
        ["active_creator_agent_session_id"],
    )

    op.create_table(
        "creation_thread_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creation_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("client_event_id", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), server_default="system", nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sequence >= 0 AND revision >= 0", name="ck_creation_thread_events_counters"
        ),
        sa.CheckConstraint(
            "role IN ('user','assistant','system')", name="ck_creation_thread_events_role"
        ),
        sa.UniqueConstraint("thread_id", "sequence", name="uq_creation_thread_events_sequence"),
        sa.UniqueConstraint("thread_id", "revision", name="uq_creation_thread_events_revision"),
        sa.UniqueConstraint(
            "thread_id", "client_event_id", name="uq_creation_thread_events_client_id"
        ),
    )
    op.create_index(
        "idx_creation_thread_events_thread_created",
        "creation_thread_events",
        ["thread_id", "created_at"],
    )

    # Make the transcript append-only at the database boundary.  Application
    # code cannot accidentally rewrite history during reconciliation or retry.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION
        creation_thread_events_append_only_guard_0092() RETURNS trigger AS $$
        BEGIN
            -- Preserve account/thread deletion cascades. The parent row is
            -- already gone when its FK action reaches this child trigger; a
            -- direct event DELETE still sees the parent and remains blocked.
            IF TG_OP = 'DELETE' AND NOT EXISTS (
                SELECT 1 FROM creation_threads WHERE id = OLD.thread_id
            ) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'creation_thread_events are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS creation_thread_events_append_only ON creation_thread_events"
    )
    op.execute(
        """
        CREATE TRIGGER creation_thread_events_append_only
        BEFORE UPDATE OR DELETE ON creation_thread_events
        FOR EACH ROW EXECUTE FUNCTION creation_thread_events_append_only_guard_0092()
        """
    )

    # Project only unscheduled, creator-edited drafts.  JSONB is a projection,
    # not a second render state machine; clip paths are retained only as media
    # entries for the old draft so the new client can show its count.
    op.execute(
        """
        INSERT INTO creation_threads
            (creator_id, status, revision, state, content_plan_id,
             active_plan_item_id, active_job_id)
        SELECT
            cp.user_id,
            'active',
            0,
            jsonb_build_object(
                'edit_format', COALESCE(pi.edit_format, 'montage'),
                'audio_mode', COALESCE(pi.audio_mode, 'kria'),
                'intent', COALESCE(NULLIF(pi.idea, ''), 'Untitled video'),
                'media', COALESCE(
                    (SELECT jsonb_agg(jsonb_build_object(
                        'media_id', 'legacy-' || md5(path),
                        'kind', 'video'
                    )) FROM jsonb_array_elements_text(
                        COALESCE(pi.clip_gcs_paths, '[]'::jsonb)
                    ) AS paths(path)),
                    '[]'::jsonb
                ),
                'media_count', jsonb_array_length(COALESCE(pi.clip_gcs_paths, '[]'::jsonb))
            ),
            cp.id,
            pi.id,
            pi.current_job_id
        FROM plan_items pi
        JOIN content_plans cp ON cp.id = pi.content_plan_id
        WHERE pi.day_index IS NULL
          AND pi.user_edited IS TRUE
          AND NOT EXISTS (
              SELECT 1 FROM creation_threads existing
              WHERE existing.active_plan_item_id = pi.id
          )
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS creation_thread_events_append_only ON creation_thread_events"
    )
    op.execute("DROP FUNCTION IF EXISTS creation_thread_events_append_only_guard_0092()")
    op.drop_index("idx_creation_thread_events_thread_created", table_name="creation_thread_events")
    op.drop_table("creation_thread_events")
    op.drop_index("idx_creation_threads_active_session", table_name="creation_threads")
    op.drop_index("idx_creation_threads_active_job", table_name="creation_threads")
    op.drop_index("idx_creation_threads_creator_updated", table_name="creation_threads")
    op.drop_table("creation_threads")
