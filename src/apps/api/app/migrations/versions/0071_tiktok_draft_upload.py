"""Add TikTok upload-to-drafts delivery mode.

Revision ID: 0071
Revises: 0070
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tiktok_publications",
        sa.Column(
            "delivery_mode",
            sa.Text(),
            nullable=False,
            server_default="direct_post",
        ),
    )
    op.create_check_constraint(
        "ck_tiktok_pub_delivery_mode",
        "tiktok_publications",
        "delivery_mode IN ('direct_post','draft_upload')",
    )
    op.drop_constraint(
        "ck_tiktok_pub_visibility_status",
        "tiktok_publications",
        type_="check",
    )
    op.create_check_constraint(
        "ck_tiktok_pub_visibility_status",
        "tiktok_publications",
        "visibility_status IN ('unknown','draft','private','public','removed')",
    )


def downgrade() -> None:
    draft_rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM tiktok_publications "
                "WHERE delivery_mode = 'draft_upload' OR visibility_status = 'draft'"
            )
        )
        .scalar_one()
    )
    if draft_rows:
        raise RuntimeError("Cannot downgrade 0071 while TikTok draft-upload records exist")
    op.drop_constraint(
        "ck_tiktok_pub_visibility_status",
        "tiktok_publications",
        type_="check",
    )
    op.create_check_constraint(
        "ck_tiktok_pub_visibility_status",
        "tiktok_publications",
        "visibility_status IN ('unknown','private','public','removed')",
    )
    op.drop_constraint(
        "ck_tiktok_pub_delivery_mode",
        "tiktok_publications",
        type_="check",
    )
    op.drop_column("tiktok_publications", "delivery_mode")
