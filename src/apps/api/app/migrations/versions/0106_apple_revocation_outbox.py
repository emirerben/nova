"""Durable Apple credential-revocation debt for account erasure.

Revision ID: 0106
Revises: 0105
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "apple_revocation_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("encrypted_refresh_token", sa.LargeBinary(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("lease_until", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_error_code", sa.Text()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_apple_revocation_outbox_attempts_nonnegative"),
    )
    op.create_index(
        "idx_apple_revocation_outbox_due", "apple_revocation_outbox", ["next_attempt_at"]
    )
    op.create_index("idx_apple_revocation_outbox_lease", "apple_revocation_outbox", ["lease_until"])


def downgrade() -> None:
    op.drop_table("apple_revocation_outbox")
