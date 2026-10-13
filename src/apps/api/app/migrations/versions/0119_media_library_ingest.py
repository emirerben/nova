"""Owner-scoped library upload reservations and durable analysis outbox.

Revision ID: 0119
Revises: 0118
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0119"
down_revision = "0118"
branch_labels = None
depends_on = None

_REFERENCE_TABLES = (
    "jobs",
    "job_clips",
    "content_plans",
    "plan_items",
    "plan_item_assets",
    "personas",
    "tiktok_publications",
)


def upgrade() -> None:
    op.add_column("plan_item_assets", sa.Column("library_asset_id", postgresql.UUID()))
    op.add_column("plan_item_assets", sa.Column("library_source_id", postgresql.UUID()))
    op.create_foreign_key(
        "fk_plan_item_asset_library_owner",
        "plan_item_assets",
        "media_assets",
        ["library_asset_id", "user_id"],
        ["id", "user_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_plan_item_asset_library_source",
        "plan_item_assets",
        "media_asset_sources",
        ["library_source_id", "user_id", "library_asset_id"],
        ["id", "user_id", "asset_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_plan_item_asset_library_pair",
        "plan_item_assets",
        "(library_asset_id IS NULL) = (library_source_id IS NULL)",
    )
    op.create_table(
        "media_asset_upload_reservations",
        sa.Column(
            "id", postgresql.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_upload_id", sa.Text(), nullable=False),
        sa.Column("request_json", postgresql.JSONB(), nullable=False),
        sa.Column("object_path", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="reserved"),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID()),
        sa.Column("source_id", postgresql.UUID()),
        sa.Column("client_event_id", sa.Text()),
        sa.Column(
            "analysis_dispatch_pending", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("user_id", "client_upload_id", name="uq_media_upload_owner_client"),
        sa.ForeignKeyConstraint(
            ["asset_id", "user_id"],
            ["media_assets.id", "media_assets.user_id"],
            name="fk_media_upload_asset_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "user_id", "asset_id"],
            [
                "media_asset_sources.id",
                "media_asset_sources.user_id",
                "media_asset_sources.asset_id",
            ],
            name="fk_media_upload_source_owner",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("status IN ('reserved','finalized')", name="ck_media_upload_status"),
        sa.CheckConstraint(
            "(status = 'reserved' AND asset_id IS NULL AND source_id IS NULL) OR "
            "(status = 'finalized' AND asset_id IS NOT NULL AND source_id IS NOT NULL)",
            name="ck_media_upload_finalization",
        ),
    )
    op.create_index(
        "idx_media_upload_owner_expiry",
        "media_asset_upload_reservations",
        ["user_id", "status", "expires_at"],
    )
    op.create_index(
        "idx_media_upload_analysis_outbox",
        "media_asset_upload_reservations",
        ["analysis_dispatch_pending", "created_at"],
    )
    # The existing 0103 BEFORE trigger acquires the owner fence first (its
    # name sorts before this trigger). Cover every legacy reference writer,
    # including those that do not know about library upload receipts.
    op.execute("""
        CREATE FUNCTION guard_temporary_duplicate_reference_0119()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE owner_id uuid;
        BEGIN
            IF TG_TABLE_NAME = 'plan_items' THEN
                SELECT user_id INTO owner_id FROM content_plans WHERE id = NEW.content_plan_id;
            ELSIF TG_TABLE_NAME = 'job_clips' THEN
                SELECT user_id INTO owner_id FROM jobs WHERE id = NEW.job_id;
            ELSE
                owner_id := NEW.user_id;
            END IF;
            IF owner_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM temporary_media_uploads receipt
                WHERE receipt.user_id = owner_id AND receipt.purpose = 'library_duplicate'
                  AND (receipt.status IN ('cleanup_pending', 'deleted')
                       OR receipt.cleanup_claimed_at IS NOT NULL OR receipt.deleted_at IS NOT NULL)
                  AND (project_media_row_names_path_0103(to_jsonb(NEW), receipt.object_path)
                       OR (TG_TABLE_NAME = 'plan_items' AND project_media_job_names_path_0103(
                           NULLIF(to_jsonb(NEW)->>'current_job_id', '')::uuid,
                           receipt.object_path)))
                  AND CASE WHEN TG_OP = 'INSERT' THEN true ELSE NOT (
                      project_media_row_names_path_0103(to_jsonb(OLD), receipt.object_path)
                      OR (TG_TABLE_NAME = 'plan_items' AND project_media_job_names_path_0103(
                          NULLIF(to_jsonb(OLD)->>'current_job_id', '')::uuid, receipt.object_path)))
                      END
            ) THEN
                RAISE EXCEPTION 'project media reference raced with temporary upload cleanup'
                    USING ERRCODE = '40001';
            END IF;
            RETURN NEW;
        END $$
    """)
    for table in _REFERENCE_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_temporary_duplicate_fence_0119 "
            f"BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW "
            "EXECUTE FUNCTION guard_temporary_duplicate_reference_0119()"
        )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM media_asset_upload_reservations) OR
           EXISTS (SELECT 1 FROM plan_item_assets WHERE library_asset_id IS NOT NULL) OR
           EXISTS (SELECT 1 FROM temporary_media_uploads WHERE purpose = 'library_duplicate') THEN
            RAISE EXCEPTION 'Refusing to discard media-library upload receipts';
        END IF;
    END $$""")
    for table in reversed(_REFERENCE_TABLES):
        op.execute(f"DROP TRIGGER trg_{table}_temporary_duplicate_fence_0119 ON {table}")
    op.execute("DROP FUNCTION guard_temporary_duplicate_reference_0119()")
    op.drop_table("media_asset_upload_reservations")
    op.drop_constraint("ck_plan_item_asset_library_pair", "plan_item_assets")
    op.drop_constraint("fk_plan_item_asset_library_source", "plan_item_assets", type_="foreignkey")
    op.drop_constraint("fk_plan_item_asset_library_owner", "plan_item_assets", type_="foreignkey")
    op.drop_column("plan_item_assets", "library_source_id")
    op.drop_column("plan_item_assets", "library_asset_id")
