"""Sound-effect category, search terms and curated rank for the creator library (KRI-173).

Revision ID: 0109
Revises: 0108
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sound_effects", sa.Column("category", sa.Text(), nullable=True))
    op.add_column(
        "sound_effects",
        sa.Column("search_terms", pg.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column("sound_effects", sa.Column("catalog_rank", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("sound_effects", "catalog_rank")
    op.drop_column("sound_effects", "search_terms")
    op.drop_column("sound_effects", "category")
