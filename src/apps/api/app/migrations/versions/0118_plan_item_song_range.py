"""The part of the song the creator filmed in (KRI-580).

Most songs repeat their verses, so a take's words match the song at every repeat. The creator
can say which stretch they used; takes are then matched only inside it. Both columns are NULL
(match against the whole song) or both set. Cleared whenever the song changes.
"""

import sqlalchemy as sa
from alembic import op

revision = "0118"
down_revision = "0117"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plan_items", sa.Column("song_range_start_s", sa.Float(), nullable=True))
    op.add_column("plan_items", sa.Column("song_range_end_s", sa.Float(), nullable=True))
    op.create_check_constraint(
        "ck_plan_items_song_range",
        "plan_items",
        "(song_range_start_s IS NULL AND song_range_end_s IS NULL) OR "
        "(song_range_start_s IS NOT NULL AND song_range_end_s IS NOT NULL "
        "AND song_range_start_s >= 0 AND song_range_end_s > song_range_start_s)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_plan_items_song_range", "plan_items", type_="check")
    op.drop_column("plan_items", "song_range_end_s")
    op.drop_column("plan_items", "song_range_start_s")
