"""Serialize retention references per owner and claim billing alerts.

Revision ID: 0103
Revises: 0102
Create Date: 2026-09-08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None

_REFERENCE_TABLES = (
    "jobs",
    "job_clips",
    "plan_items",
    "plan_item_assets",
    "content_plans",
    "personas",
    "tiktok_publications",
)


def upgrade() -> None:
    op.add_column(
        "ai_cost_reservations",
        sa.Column("provider_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE ai_cost_reservations SET provider_started_at = created_at "
        "WHERE status IN ('provider_started', 'settled', 'unknown')"
    )
    op.create_index(
        "idx_ai_cost_reservations_settled_provider_started_at",
        "ai_cost_reservations",
        ["provider_started_at"],
        postgresql_where=sa.text("status = 'settled'"),
    )
    op.add_column(
        "billing_reconciliations",
        sa.Column("alert_claim_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "billing_reconciliations",
        sa.Column("alert_claim_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_billing_reconciliations_alert_claim",
        "billing_reconciliations",
        ["alert_claim_expires_at"],
        postgresql_where=sa.text("alerted_at IS NULL AND alert_claim_id IS NOT NULL"),
    )
    op.add_column(
        "storage_retention_entries",
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.drop_constraint(
        "ck_storage_retention_entry_status",
        "storage_retention_entries",
        type_="check",
    )
    op.create_check_constraint(
        "ck_storage_retention_entry_status",
        "storage_retention_entries",
        "status IN ('pending','deleting','deleted','skipped','failed')",
    )

    # Cached ClipMeta may contain transcript text. Scope the durable tier to
    # its creator so account deletion cascades it, and drop pre-release rows
    # that have no trustworthy owner attribution.
    op.drop_constraint(
        "uq_media_analysis_cache_identity",
        "media_analysis_cache",
        type_="unique",
    )
    op.execute("DELETE FROM media_analysis_cache")
    op.add_column(
        "media_analysis_cache",
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_media_analysis_cache_owner_identity",
        "media_analysis_cache",
        [
            "creator_id",
            "source_identity",
            "analyzer",
            "model",
            "prompt_version",
            "schema_version",
        ],
    )
    op.create_index(
        "idx_media_analysis_cache_creator",
        "media_analysis_cache",
        ["creator_id"],
    )

    # A 60-bit MD5 prefix is available in core PostgreSQL and exactly matches
    # project_media_reference_lock_key(). This is a coordination hash only.
    op.execute(
        """
        CREATE FUNCTION project_media_reference_lock_key_0103(owner_id uuid)
        RETURNS bigint
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        AS $$
            SELECT (
                ('x' || substr(md5('project-media:' || owner_id::text), 1, 15))::bit(60)
            )::bigint
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION project_media_row_names_path_0103(payload jsonb, object_path text)
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        AS $$
            SELECT EXISTS (
                SELECT 1
                FROM jsonb_path_query(
                    payload, '$.** ? (@.type() == "string")'
                ) AS values(value)
                WHERE value #>> '{}' = object_path
                   OR (
                       (value #>> '{}') LIKE 'https://storage.googleapis.com/%'
                       AND strpos(split_part(value #>> '{}', '?', 1), '/' || object_path) > 0
                   )
                   OR (
                       (value #>> '{}') LIKE 'https://storage.cloud.google.com/%'
                       AND strpos(split_part(value #>> '{}', '?', 1), '/' || object_path) > 0
                   )
            )
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION project_media_job_names_path_0103(
            candidate_job_id uuid,
            object_path text
        )
        RETURNS boolean
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        AS $$
            SELECT candidate_job_id IS NOT NULL AND (
                EXISTS (
                    SELECT 1
                    FROM jobs job
                    WHERE job.id = candidate_job_id
                      AND project_media_row_names_path_0103(to_jsonb(job), object_path)
                )
                OR EXISTS (
                    SELECT 1
                    FROM job_clips clip
                    WHERE clip.job_id = candidate_job_id
                      AND project_media_row_names_path_0103(to_jsonb(clip), object_path)
                )
            )
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lock_project_media_reference_write_0103()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            old_owner uuid;
            new_owner uuid;
            owner_id uuid;
        BEGIN
            IF TG_TABLE_NAME IN (
                'jobs',
                'plan_item_assets',
                'content_plans',
                'personas',
                'tiktok_publications'
            ) THEN
                IF TG_OP <> 'INSERT' THEN
                    old_owner := OLD.user_id;
                END IF;
                IF TG_OP <> 'DELETE' THEN
                    new_owner := NEW.user_id;
                END IF;
            ELSIF TG_TABLE_NAME = 'job_clips' THEN
                IF TG_OP <> 'INSERT' THEN
                    SELECT user_id INTO old_owner FROM jobs WHERE id = OLD.job_id;
                END IF;
                IF TG_OP <> 'DELETE' THEN
                    SELECT user_id INTO new_owner FROM jobs WHERE id = NEW.job_id;
                END IF;
            ELSIF TG_TABLE_NAME = 'plan_items' THEN
                IF TG_OP <> 'INSERT' THEN
                    SELECT user_id INTO old_owner FROM content_plans WHERE id = OLD.content_plan_id;
                END IF;
                IF TG_OP <> 'DELETE' THEN
                    SELECT user_id INTO new_owner FROM content_plans WHERE id = NEW.content_plan_id;
                END IF;
            ELSE
                RAISE EXCEPTION 'unsupported project-media reference table: %', TG_TABLE_NAME;
            END IF;

            FOR owner_id IN
                SELECT DISTINCT candidate
                FROM unnest(ARRAY[old_owner, new_owner]) AS owners(candidate)
                WHERE candidate IS NOT NULL
                ORDER BY candidate
            LOOP
                PERFORM pg_advisory_xact_lock(project_media_reference_lock_key_0103(owner_id));
            END LOOP;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;

            -- A writer can validate an object before it reaches this trigger.
            -- If retention deleted that path after the writer's transaction
            -- began, reject a newly introduced reference after waiting for the
            -- shared owner lock. A later transaction may safely reuse the key
            -- after validating a newly uploaded generation.
            IF new_owner IS NOT NULL AND EXISTS (
                SELECT 1
                FROM storage_retention_entries entry
                WHERE entry.creator_id = new_owner
                  AND entry.action = 'delete'
                  AND entry.status IN ('deleting', 'deleted')
                  AND (
                      entry.status = 'deleting'
                      OR entry.deleted_at >= transaction_timestamp()
                      OR entry.deleted_at >= clock_timestamp() - interval '1 hour'
                  )
                  AND (
                      project_media_row_names_path_0103(
                          to_jsonb(NEW), entry.object_path
                      )
                      OR (
                          TG_TABLE_NAME = 'plan_items'
                          AND project_media_job_names_path_0103(
                              NULLIF(to_jsonb(NEW)->>'current_job_id', '')::uuid,
                              entry.object_path
                          )
                      )
                  )
                  AND CASE
                      WHEN TG_OP = 'INSERT' THEN true
                      ELSE NOT (
                          project_media_row_names_path_0103(
                              to_jsonb(OLD), entry.object_path
                          )
                          OR (
                              TG_TABLE_NAME = 'plan_items'
                              AND project_media_job_names_path_0103(
                                  NULLIF(to_jsonb(OLD)->>'current_job_id', '')::uuid,
                                  entry.object_path
                              )
                          )
                      )
                  END
            ) THEN
                RAISE EXCEPTION 'project media reference raced with retention deletion'
                    USING ERRCODE = '40001';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    for table in _REFERENCE_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_project_media_lock_0103 "
            f"BEFORE INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION lock_project_media_reference_write_0103()"
        )


def downgrade() -> None:
    for table in reversed(_REFERENCE_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_project_media_lock_0103 ON {table}")
    op.execute("DROP FUNCTION IF EXISTS lock_project_media_reference_write_0103()")
    op.execute("DROP FUNCTION IF EXISTS project_media_job_names_path_0103(uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS project_media_row_names_path_0103(jsonb, text)")
    op.execute("DROP FUNCTION IF EXISTS project_media_reference_lock_key_0103(uuid)")
    op.drop_index("idx_media_analysis_cache_creator", table_name="media_analysis_cache")
    op.drop_constraint(
        "uq_media_analysis_cache_owner_identity",
        "media_analysis_cache",
        type_="unique",
    )
    # The cache is expendable. Multiple creators may legally have identical
    # cache identities under 0103, so clear rows before restoring 0102's
    # global uniqueness rather than making downgrade depend on live data.
    op.execute("DELETE FROM media_analysis_cache")
    op.drop_column("media_analysis_cache", "creator_id")
    op.create_unique_constraint(
        "uq_media_analysis_cache_identity",
        "media_analysis_cache",
        ["source_identity", "analyzer", "model", "prompt_version", "schema_version"],
    )
    op.drop_index(
        "idx_billing_reconciliations_alert_claim",
        table_name="billing_reconciliations",
    )
    op.drop_column("billing_reconciliations", "alert_claim_expires_at")
    op.drop_column("billing_reconciliations", "alert_claim_id")
    op.drop_constraint(
        "ck_storage_retention_entry_status",
        "storage_retention_entries",
        type_="check",
    )
    # 0102 cannot represent an in-flight durable tombstone. Preserve the safe
    # failure posture (possible object leak, never an untracked deletion) when
    # rolling back code while a worker is between remote I/O phases.
    op.execute("UPDATE storage_retention_entries SET status = 'failed' WHERE status = 'deleting'")
    op.create_check_constraint(
        "ck_storage_retention_entry_status",
        "storage_retention_entries",
        "status IN ('pending','deleted','skipped','failed')",
    )
    op.drop_column("storage_retention_entries", "deleted_at", if_exists=True)
    op.drop_index(
        "idx_ai_cost_reservations_settled_provider_started_at",
        table_name="ai_cost_reservations",
        if_exists=True,
    )
    # Local dev/test databases may have applied an earlier pre-release draft
    # of 0103. Its temporary created_at index is safe to remove on downgrade.
    op.drop_index(
        "idx_ai_cost_reservations_settled_created_at",
        table_name="ai_cost_reservations",
        if_exists=True,
    )
    op.drop_column("ai_cost_reservations", "provider_started_at", if_exists=True)
