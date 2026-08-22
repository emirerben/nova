"""Separate pool media readiness from AI analysis state.

Manual overlay cards must wait for a storage-verified decode/probe (and, for
videos/HEIF, a browser preview), but AI suggestions still need the existing
``status=ready`` analysis lifecycle.  The server fingerprint also removes the
large, blocking client-side SHA-256 from the trust boundary.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plan_item_assets", sa.Column("content_fingerprint", sa.Text(), nullable=True))
    op.add_column(
        "plan_item_assets",
        sa.Column("media_status", sa.Text(), nullable=False, server_default="pending"),
    )
    op.add_column(
        "plan_item_assets", sa.Column("preview_gcs_generation", sa.Text(), nullable=True)
    )
    op.add_column(
        "plan_item_assets",
        sa.Column(
            "deduplicated_to_asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plan_item_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Existing rows that already passed the old worker's probe are safe for
    # manual use.  Transitional/failed rows remain pending until reconciled.
    op.execute(
        sa.text(
            "UPDATE plan_item_assets SET media_status = 'ready' WHERE status = 'ready'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE plan_item_assets SET media_status = 'failed' "
            "WHERE status = 'failed' AND media_status = 'pending'"
        )
    )
    op.create_index(
        "idx_plan_item_assets_content_fingerprint",
        "plan_item_assets",
        ["plan_item_id", "content_fingerprint"],
        postgresql_where=sa.text("content_fingerprint IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_plan_item_assets_content_fingerprint", table_name="plan_item_assets")
    op.drop_column("plan_item_assets", "deduplicated_to_asset_id")
    op.drop_column("plan_item_assets", "preview_gcs_generation")
    op.drop_column("plan_item_assets", "media_status")
    op.drop_column("plan_item_assets", "content_fingerprint")
