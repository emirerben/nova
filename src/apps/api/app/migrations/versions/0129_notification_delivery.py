"""Inactive native notification registrations and durable delivery outcomes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notification_installations",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("family_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("encrypted_token", pg.BYTEA(), nullable=True),
        sa.Column("token_fingerprint", sa.String(64), nullable=False),
        sa.Column("registration_version", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "environment IN ('sandbox', 'production') AND registration_version > 0",
            name="ck_notification_installation_target",
        ),
        sa.CheckConstraint(
            "revoked_at IS NOT NULL OR encrypted_token IS NOT NULL",
            name="ck_notification_installation_token",
        ),
    )
    op.create_index(
        "idx_notification_installation_owner", "notification_installations", ["owner_id"]
    )
    op.create_index(
        "uq_notification_installation_active_target",
        "notification_installations",
        ["environment", "topic", "token_fingerprint"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "activity_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("native_activities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "installation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("notification_installations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("registration_version", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("last_result_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("activity_id", "installation_id", name="uq_notification_delivery_pair"),
        sa.CheckConstraint(
            "status IN ('pending','leased','accepted','suppressed',"
            "'rejected','exhausted','uncertain')",
            name="ck_notification_delivery_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND registration_version > 0", name="ck_notification_delivery_attempts"
        ),
        sa.CheckConstraint(
            "(status = 'leased' AND lease_id IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status != 'leased' AND lease_id IS NULL AND lease_until IS NULL)",
            name="ck_notification_delivery_lease",
        ),
    )
    op.create_index(
        "idx_notification_delivery_due", "notification_deliveries", ["status", "available_at"]
    )


def downgrade():
    op.drop_table("notification_deliveries")
    op.drop_table("notification_installations")
