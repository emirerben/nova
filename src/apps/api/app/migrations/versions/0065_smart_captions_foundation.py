"""Add Smart Captions creator assignment and per-item choice.

Revision ID: 0065
Revises: 0064
Create Date: 2026-07-17

Quality Core persists the compiled Smart document on the existing variant JSON.
Revision/outbox tables intentionally wait until a correction workflow consumes
them. Existing plan items receive ``smart_captions_enabled=false`` without a
data backfill.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column(
            "smart_captions_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "plan_items",
        sa.Column(
            "smart_sound_design_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_check_constraint(
        "ck_plan_items_smart_captions_format",
        "plan_items",
        "NOT smart_captions_enabled OR COALESCE(edit_format, '') = 'subtitled'",
    )
    op.create_table(
        "creator_style_assignments",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("preset_id", sa.Text(), nullable=False),
        sa.Column("preset_version", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("assigned_by", sa.Text(), nullable=False, server_default="system"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column("sound_effects", sa.Column("sha256", sa.Text(), nullable=True))
    op.add_column("sound_effects", sa.Column("analysis_version", sa.Text(), nullable=True))
    op.add_column(
        "sound_effects",
        sa.Column(
            "role_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    for column in (
        "integrated_lufs",
        "true_peak_dbtp",
        "attack_ms",
        "decay_ms",
        "energy",
        "brightness",
        "vocal_probability",
    ):
        op.add_column("sound_effects", sa.Column(column, sa.Float(), nullable=True))
    op.add_column("sound_effects", sa.Column("contains_voice", sa.Boolean(), nullable=True))
    op.add_column("sound_effects", sa.Column("provenance", sa.Text(), nullable=True))
    op.add_column("sound_effects", sa.Column("license", sa.Text(), nullable=True))
    op.add_column("sound_effects", sa.Column("quality_tier", sa.Text(), nullable=True))
    op.add_column(
        "sound_effects",
        sa.Column(
            "manual_audit_status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
    )
    op.create_check_constraint(
        "ck_sound_effects_vocal_probability",
        "sound_effects",
        "vocal_probability IS NULL OR (vocal_probability >= 0 AND vocal_probability <= 1)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_sound_effects_vocal_probability", "sound_effects", type_="check"
    )
    for column in (
        "manual_audit_status",
        "quality_tier",
        "license",
        "provenance",
        "vocal_probability",
        "contains_voice",
        "brightness",
        "energy",
        "decay_ms",
        "attack_ms",
        "true_peak_dbtp",
        "integrated_lufs",
        "role_tags",
        "analysis_version",
        "sha256",
    ):
        op.drop_column("sound_effects", column)
    op.drop_table("creator_style_assignments")
    op.drop_constraint(
        "ck_plan_items_smart_captions_format",
        "plan_items",
        type_="check",
    )
    op.drop_column("plan_items", "smart_captions_enabled")
    op.drop_column("plan_items", "smart_sound_design_enabled")
