"""Add voiceover_caption_style to plan_items for the narrated caption style.

Revision ID: 0061
Revises: 0060
Create Date: 2026-06-24

Changes:
  plan_items.voiceover_caption_style: new Text nullable — caption style for the
    narrated archetype: "sentence" (default, sentence-block captions) or "word"
    (one big word at a time, the "qbuilder" word-by-word look). Set via
    PATCH /plan-items/{id}/voiceover-caption-style; threaded to
    build_generative_job so the narrated render burns the chosen style.
    NULL = "sentence" (today's behavior) at render time.

No backfill needed (existing rows stay NULL = sentence captions, unchanged).
"""

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_items",
        sa.Column("voiceover_caption_style", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_items", "voiceover_caption_style")
