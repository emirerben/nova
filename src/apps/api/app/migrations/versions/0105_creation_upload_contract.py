"""Bind project uploads to immutable source/proxy provenance.

Revision ID: 0105
Revises: 0104
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL is the legacy cloud-source contract. Existing signed PUT windows keep
    # their existing semantics; new proxy reservations always persist a binding.
    op.add_column(
        "creation_thread_upload_reservations",
        sa.Column("upload_contract", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM creation_thread_upload_reservations "
                "WHERE upload_contract->>'purpose' = 'analysis_proxy' LIMIT 1"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Cannot remove provenance while proxy reservations remain")
    op.drop_column("creation_thread_upload_reservations", "upload_contract")
