"""Add verified native identities and rotating mobile refresh sessions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mobile_identities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("email_verified", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("provider", "subject", name="uq_mobile_identity_provider_subject"),
    )
    op.create_index("idx_mobile_identity_user", "mobile_identities", ["user_id"])
    op.create_table(
        "mobile_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("token_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "replaced_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mobile_sessions.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint("token_hash", name="uq_mobile_sessions_token_hash"),
    )
    op.create_index("idx_mobile_session_user", "mobile_sessions", ["user_id"])
    op.create_index("idx_mobile_session_family", "mobile_sessions", ["family_id"])
    op.create_index("idx_mobile_session_expiry", "mobile_sessions", ["expires_at"])
    op.create_table(
        "temporary_media_uploads",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("object_path", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="reserved", nullable=False),
        sa.Column("retention_expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("cleanup_claimed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("delete_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("object_path", name="uq_temporary_media_upload_object_path"),
    )
    op.create_index("idx_temporary_media_upload_user", "temporary_media_uploads", ["user_id"])
    op.create_index(
        "idx_temporary_media_upload_cleanup",
        "temporary_media_uploads",
        ["status", "retention_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_temporary_media_upload_cleanup", table_name="temporary_media_uploads")
    op.drop_index("idx_temporary_media_upload_user", table_name="temporary_media_uploads")
    op.drop_table("temporary_media_uploads")
    op.drop_index("idx_mobile_session_expiry", table_name="mobile_sessions")
    op.drop_index("idx_mobile_session_family", table_name="mobile_sessions")
    op.drop_index("idx_mobile_session_user", table_name="mobile_sessions")
    op.drop_table("mobile_sessions")
    op.drop_index("idx_mobile_identity_user", table_name="mobile_identities")
    op.drop_table("mobile_identities")
