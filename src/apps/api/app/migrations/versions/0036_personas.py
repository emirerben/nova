"""Create personas table (content-plan Phase 2, data model).

Revision ID: 0036
Revises: 0035
Create Date: 2026-05-29

1:1 with users. Holds the onboarding questionnaire (UNTRUSTED free text) and
the editable AI-generated persona JSON. No behavior change — additive table.
The unique constraint on user_id enforces the 1:1.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personas",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("questionnaire", postgresql.JSONB(), nullable=True),
        sa.Column("persona", postgresql.JSONB(), nullable=True),
        sa.Column("persona_status", sa.Text(), nullable=False, server_default="generating"),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", name="uq_personas_user_id"),
    )


def downgrade() -> None:
    op.drop_table("personas")
