# ruff: noqa: E501
"""Add creator-owned immutable media-library contracts (KRI-384).

Revision ID: 0117
Revises: 0116
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None


def _uuid(name: str, *args: object, **kwargs: object) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, **kwargs)


def upgrade() -> None:
    op.create_table(
        "media_assets",
        _uuid("id", primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _uuid("user_id", sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("original_declared_sha256", sa.Text()),
        sa.Column("original_byte_count", sa.BigInteger()),
        sa.Column("original_availability", sa.Text(), nullable=False, server_default="device_only"),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("creator_context", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint("id", "user_id", name="uq_media_assets_id_user"),
        sa.CheckConstraint("kind IN ('video','image','audio')", name="ck_media_assets_kind"),
        sa.CheckConstraint(
            "original_availability IN ('device_only','cloud_backed')",
            name="ck_media_assets_original_availability",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','ready','failed')", name="ck_media_assets_status"
        ),
        sa.CheckConstraint(
            "original_byte_count IS NULL OR original_byte_count >= 0", name="ck_media_assets_bytes"
        ),
    )
    op.create_index("idx_media_assets_user_created", "media_assets", ["user_id", "created_at"])
    op.create_table(
        "media_collections",
        _uuid("id", primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _uuid("user_id", sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("id", "user_id", name="uq_media_collections_id_user"),
    )
    op.create_index(
        "idx_media_collections_user_updated", "media_collections", ["user_id", "updated_at"]
    )
    op.create_table(
        "media_asset_sources",
        _uuid("id", primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _uuid("user_id", sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        _uuid("asset_id", nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("object_path", sa.Text(), nullable=False),
        sa.Column("object_generation", sa.Text(), nullable=False),
        sa.Column("uploaded_sha256", sa.Text()),
        sa.Column("byte_count", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="available"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True)),
        sa.ForeignKeyConstraint(
            ["asset_id", "user_id"],
            ["media_assets.id", "media_assets.user_id"],
            name="fk_media_asset_sources_asset_owner",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "id", "user_id", "asset_id", name="uq_media_asset_sources_id_owner_asset"
        ),
        sa.UniqueConstraint(
            "user_id",
            "object_path",
            "object_generation",
            name="uq_media_asset_sources_object_generation",
        ),
        sa.CheckConstraint(
            "role IN ('original','analysis_proxy','thumbnail')", name="ck_media_asset_sources_role"
        ),
        sa.CheckConstraint(
            "status IN ('available','missing','deleting','deleted')",
            name="ck_media_asset_sources_status",
        ),
        sa.CheckConstraint("byte_count >= 0", name="ck_media_asset_sources_bytes"),
    )
    op.create_index(
        "idx_media_asset_sources_asset",
        "media_asset_sources",
        ["user_id", "asset_id", "created_at"],
    )
    op.create_table(
        "media_collection_members",
        _uuid("collection_id", primary_key=True),
        _uuid("asset_id", primary_key=True),
        _uuid("user_id", sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["collection_id", "user_id"],
            ["media_collections.id", "media_collections.user_id"],
            name="fk_media_collection_members_collection_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "user_id"],
            ["media_assets.id", "media_assets.user_id"],
            name="fk_media_collection_members_asset_owner",
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "media_asset_references",
        _uuid("id", primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _uuid("user_id", sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        _uuid("asset_id", nullable=False),
        _uuid("source_id"),
        _uuid("creation_thread_id", sa.ForeignKey("creation_threads.id", ondelete="CASCADE")),
        _uuid("plan_item_id", sa.ForeignKey("plan_items.id", ondelete="CASCADE")),
        _uuid("job_id", sa.ForeignKey("jobs.id", ondelete="CASCADE")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "user_id"],
            ["media_assets.id", "media_assets.user_id"],
            name="fk_media_asset_references_asset_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "user_id", "asset_id"],
            [
                "media_asset_sources.id",
                "media_asset_sources.user_id",
                "media_asset_sources.asset_id",
            ],
            name="fk_media_asset_references_source_owner_asset",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(creation_thread_id IS NOT NULL)::int + (plan_item_id IS NOT NULL)::int + (job_id IS NOT NULL)::int = 1",
            name="ck_media_asset_references_one_consumer",
        ),
    )
    op.create_index(
        "idx_media_asset_references_asset",
        "media_asset_references",
        ["user_id", "asset_id", "created_at"],
    )
    op.create_table(
        "media_asset_analyses",
        _uuid("id", primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _uuid("user_id", sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        _uuid("asset_id", nullable=False),
        _uuid("source_id", nullable=False),
        sa.Column("analyzer", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("source_identity", sa.Text(), nullable=False),
        sa.Column("result_json", postgresql.JSONB()),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        _uuid("attempt_token"),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "user_id"],
            ["media_assets.id", "media_assets.user_id"],
            name="fk_media_asset_analyses_asset_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "user_id", "asset_id"],
            [
                "media_asset_sources.id",
                "media_asset_sources.user_id",
                "media_asset_sources.asset_id",
            ],
            name="fk_media_asset_analyses_source_owner_asset",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('queued','analyzing','ready','failed')",
            name="ck_media_asset_analyses_status",
        ),
        sa.UniqueConstraint(
            "user_id",
            "asset_id",
            "source_id",
            "analyzer",
            "model",
            "prompt_version",
            "schema_version",
            name="uq_media_asset_analyses_version",
        ),
    )
    op.create_index(
        "idx_media_asset_analyses_lease", "media_asset_analyses", ["status", "lease_expires_at"]
    )

    # Reuse the 0103 owner lock: source registration and reference attachment
    # serialize with retention's owner-wide manifest work. CreationThread's
    # owner column is named creator_id in the established schema.
    op.execute("""
        CREATE FUNCTION lock_media_library_source_write_0117() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE owner_id uuid; candidate_path text; candidate_generation text;
        BEGIN
            owner_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.user_id ELSE NEW.user_id END;
            PERFORM pg_advisory_xact_lock(project_media_reference_lock_key_0103(owner_id));
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            IF NEW.object_path NOT LIKE 'users/' || NEW.user_id::text || '/%'
               AND NEW.object_path NOT LIKE NEW.user_id::text || '/%' THEN
                RAISE EXCEPTION 'media source must use a durable owner prefix';
            END IF;
            IF NEW.object_path ~ '(^|/)[.][.](/|$)' OR NEW.object_path LIKE '%//%'
               OR NEW.object_path ~ ('^(users/)?' || NEW.user_id::text || '/(dev-user|staging|temporary|upload|uploads|preview|cache)(/|$)')
               OR NEW.object_generation = ''
               OR (NEW.uploaded_sha256 IS NOT NULL AND NEW.uploaded_sha256 !~ '^[0-9a-f]{64}$') THEN
                RAISE EXCEPTION 'media source has an invalid durable identity';
            END IF;
            IF TG_OP = 'UPDATE' AND (NEW.object_path IS DISTINCT FROM OLD.object_path
                OR NEW.object_generation IS DISTINCT FROM OLD.object_generation
                OR NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.asset_id IS DISTINCT FROM OLD.asset_id
                OR NEW.role IS DISTINCT FROM OLD.role
                OR NEW.byte_count IS DISTINCT FROM OLD.byte_count OR NEW.content_type IS DISTINCT FROM OLD.content_type
                OR (OLD.uploaded_sha256 IS NOT NULL AND NEW.uploaded_sha256 IS DISTINCT FROM OLD.uploaded_sha256)) THEN
                RAISE EXCEPTION 'verified media source identity is immutable';
            END IF;
            IF (TG_OP = 'INSERT' OR NEW.status = 'available') AND EXISTS (SELECT 1 FROM storage_retention_entries entry
                WHERE entry.creator_id = NEW.user_id AND entry.action = 'delete'
                  AND entry.status IN ('deleting','deleted') AND entry.object_path = NEW.object_path
                  AND entry.object_generation = NEW.object_generation) THEN
                RAISE EXCEPTION 'media source raced with retention tombstone' USING ERRCODE = '40001';
            END IF;
            -- Temporary upload cleanup commits its owner-locked claim before
            -- deleting remote bytes. Do not admit a source that could point
            -- at that claimed or deleted object; an adopted receipt is safe.
            IF (TG_OP = 'INSERT' OR NEW.status = 'available') AND EXISTS (
                SELECT 1 FROM temporary_media_uploads receipt
                WHERE receipt.user_id = NEW.user_id AND receipt.object_path = NEW.object_path
                  AND (receipt.status IN ('cleanup_pending', 'deleted')
                       OR receipt.cleanup_claimed_at IS NOT NULL)
            ) THEN
                RAISE EXCEPTION 'media source raced with temporary upload cleanup' USING ERRCODE = '40001';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION protect_media_asset_identity_0117() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(project_media_reference_lock_key_0103(CASE WHEN TG_OP = 'DELETE' THEN OLD.user_id ELSE NEW.user_id END));
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            IF TG_OP = 'INSERT' THEN RETURN NEW; END IF;
            IF NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.kind IS DISTINCT FROM OLD.kind
               OR (OLD.original_declared_sha256 IS NOT NULL AND NEW.original_declared_sha256 IS DISTINCT FROM OLD.original_declared_sha256)
               OR (OLD.original_byte_count IS NOT NULL AND NEW.original_byte_count IS DISTINCT FROM OLD.original_byte_count) THEN
                RAISE EXCEPTION 'media asset identity is immutable once set';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION lock_media_library_reference_write_0117() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE owner_id uuid; source_path text; source_generation text;
        BEGIN
            owner_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.user_id ELSE NEW.user_id END;
            PERFORM pg_advisory_xact_lock(project_media_reference_lock_key_0103(owner_id));
            IF TG_OP = 'DELETE' THEN
                -- Removing a consumer starts a new inactivity window. A
                -- source pinned for months must receive a fresh warning.
                UPDATE media_asset_sources SET last_used_at = clock_timestamp()
                WHERE user_id = OLD.user_id AND asset_id = OLD.asset_id
                  AND (OLD.source_id IS NULL OR id = OLD.source_id)
                  AND status IN ('available','missing');
                RETURN OLD;
            END IF;
            -- A reference's target identity is immutable. Replacing or
            -- narrowing it must be DELETE then INSERT so the previous source
            -- receives DELETE's inactivity reset before the new pin is made.
            IF TG_OP = 'UPDATE' AND (
                NEW.user_id IS DISTINCT FROM OLD.user_id
                OR NEW.asset_id IS DISTINCT FROM OLD.asset_id
                OR NEW.source_id IS DISTINCT FROM OLD.source_id
                OR NEW.creation_thread_id IS DISTINCT FROM OLD.creation_thread_id
                OR NEW.plan_item_id IS DISTINCT FROM OLD.plan_item_id
                OR NEW.job_id IS DISTINCT FROM OLD.job_id
            ) THEN
                RAISE EXCEPTION 'media reference identity is immutable';
            END IF;
            IF NEW.creation_thread_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM creation_threads t WHERE t.id = NEW.creation_thread_id AND t.creator_id = NEW.user_id) THEN
                RAISE EXCEPTION 'media reference creation thread has another owner';
            END IF;
            IF NEW.plan_item_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM plan_items i JOIN content_plans p ON p.id = i.content_plan_id WHERE i.id = NEW.plan_item_id AND p.user_id = NEW.user_id) THEN
                RAISE EXCEPTION 'media reference plan item has another owner';
            END IF;
            IF NEW.job_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = NEW.job_id AND j.user_id = NEW.user_id) THEN
                RAISE EXCEPTION 'media reference job has another owner';
            END IF;
            IF EXISTS (
                SELECT 1 FROM media_asset_sources source LEFT JOIN storage_retention_entries entry
                  ON entry.creator_id = source.user_id AND entry.object_path = source.object_path AND entry.object_generation = source.object_generation
                WHERE source.user_id = NEW.user_id AND source.asset_id = NEW.asset_id
                  AND (NEW.source_id IS NULL OR source.id = NEW.source_id)
                  -- Asset-wide use covers current sources, not retained
                  -- history of expired generations. Explicit expired-source
                  -- references remain forbidden.
                  AND (NEW.source_id IS NOT NULL OR source.status <> 'deleted')
                  AND (source.status IN ('deleting','deleted') OR (entry.action = 'delete' AND entry.status IN ('deleting','deleted')))
            ) THEN
                RAISE EXCEPTION 'media reference raced with retention tombstone' USING ERRCODE = '40001';
            END IF;
            UPDATE media_asset_sources SET last_used_at = clock_timestamp()
            WHERE user_id = NEW.user_id AND asset_id = NEW.asset_id
              AND (NEW.source_id IS NULL OR id = NEW.source_id)
              AND status IN ('available','missing');
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_media_asset_sources_lock_0117 BEFORE INSERT OR UPDATE OR DELETE ON media_asset_sources FOR EACH ROW EXECUTE FUNCTION lock_media_library_source_write_0117()"
    )
    op.execute(
        "CREATE TRIGGER trg_media_assets_identity_0117 BEFORE INSERT OR UPDATE OR DELETE ON media_assets FOR EACH ROW EXECUTE FUNCTION protect_media_asset_identity_0117()"
    )
    op.execute(
        "CREATE TRIGGER trg_media_asset_references_lock_0117 BEFORE INSERT OR UPDATE OR DELETE ON media_asset_references FOR EACH ROW EXECUTE FUNCTION lock_media_library_reference_write_0117()"
    )


def downgrade() -> None:
    # Written library rows carry durable ownership and history. Refuse to
    # silently discard them; an empty disposable database remains reversible.
    if (
        op.get_bind()
        .execute(
            sa.text("""
        SELECT EXISTS (
          SELECT 1 FROM media_assets UNION ALL SELECT 1 FROM media_asset_sources
          UNION ALL SELECT 1 FROM media_asset_references UNION ALL SELECT 1 FROM media_asset_analyses
          UNION ALL SELECT 1 FROM media_collections UNION ALL SELECT 1 FROM media_collection_members
        )
    """)
        )
        .scalar()
    ):
        raise RuntimeError("cannot downgrade 0117 while media library data exists")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_media_asset_references_lock_0117 ON media_asset_references"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_media_assets_identity_0117 ON media_assets")
    op.execute("DROP TRIGGER IF EXISTS trg_media_asset_sources_lock_0117 ON media_asset_sources")
    op.execute("DROP FUNCTION IF EXISTS lock_media_library_reference_write_0117()")
    op.execute("DROP FUNCTION IF EXISTS protect_media_asset_identity_0117()")
    op.execute("DROP FUNCTION IF EXISTS lock_media_library_source_write_0117()")
    op.drop_table("media_asset_analyses")
    op.drop_table("media_asset_references")
    op.drop_table("media_collection_members")
    op.drop_table("media_asset_sources")
    op.drop_table("media_collections")
    op.drop_table("media_assets")
