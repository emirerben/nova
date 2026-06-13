"""Add personas.idea_seeds (JSONB) + plan_items.source_idea_seed_id (Text).

Revision ID: 0054
Revises: 0053
Create Date: 2026-06-13

idea_seeds shape (per element):
  {id: str (uuid4 hex), text: str, pillar: str | null, status: "pending" | "in_plan"}

personas.idea_seeds: user-owned intent seeds that persist across plans. Empty []
  = no seeds yet -> byte-identical plan generation (no prompt block injected).

plan_items.source_idea_seed_id: references the `id` field of the Persona.idea_seeds
  entry that seeded this item. NULL = market idea-bank origin OR T5 (provenance
  population) hasn't run. Stored as plain Text (no FK) so seed deletion is safe.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "personas",
        sa.Column(
            "idea_seeds",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "plan_items",
        sa.Column("source_idea_seed_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_items", "source_idea_seed_id")
    op.drop_column("personas", "idea_seeds")
