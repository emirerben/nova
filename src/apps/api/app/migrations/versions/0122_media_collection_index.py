"""Revisioned authorized collections and source-pinned interval search (KRI-729/730)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "media_collections", sa.Column("revision", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "media_collections",
        sa.Column("selection_json", pg.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_check_constraint(
        "ck_media_collections_revision", "media_collections", "revision >= 0"
    )
    op.create_table(
        "media_source_moments",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("asset_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("fact_version", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("start_s", sa.Float(), nullable=False),
        sa.Column("end_s", sa.Float(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("evidence_json", pg.JSONB(), nullable=False),
        sa.Column("uncertain", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id", "user_id", "asset_id"],
            [
                "media_asset_sources.id",
                "media_asset_sources.user_id",
                "media_asset_sources.asset_id",
            ],
            ondelete="CASCADE",
            name="fk_media_source_moments_owned_source",
        ),
        sa.CheckConstraint(
            "start_s >= 0 AND end_s > start_s", name="ck_media_source_moments_interval"
        ),
        sa.CheckConstraint("kind IN ('visual','speech')", name="ck_media_source_moments_kind"),
    )
    op.create_index(
        "idx_media_source_moments_source_version",
        "media_source_moments",
        ["user_id", "source_id", "fact_version"],
    )
    op.execute(
        "CREATE INDEX idx_media_source_moments_search ON media_source_moments "
        "USING gin (to_tsvector('simple', text))"
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM media_collections WHERE selection_json <> '{}'::jsonb)"
        )
    ):
        raise RuntimeError("Cannot discard creator-authored collection selections on downgrade")
    op.drop_table("media_source_moments")
    op.drop_constraint("ck_media_collections_revision", "media_collections", type_="check")
    op.drop_column("media_collections", "selection_json")
    op.drop_column("media_collections", "revision")
