"""Add plan_items.clip_assignments (JSONB) and backfill shot_id into filming_guide.

Revision ID: 0052
Revises: 0051
Create Date: 2026-06-07

clip_assignments shape: [{"gcs_path": str, "shot_id": str | null}]
  - shot_id = null  →  extra-footage pool
  - shot_id = str   →  linked to a specific shot in filming_guide

filming_guide backfill: each existing shot dict gains shot_id: uuid4().hex
  so the read path can always assume IDs exist. Malformed / non-dict
  entries are skipped defensively (real rows are always dicts; the guard
  exists to prevent a Fly release-command abort on any stale data).
"""

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add clip_assignments column.
    op.add_column(
        "plan_items",
        sa.Column(
            "clip_assignments",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )

    # 2. Backfill shot_id into every existing filming_guide entry.
    #    We do this in Python (via a connection) rather than pure SQL because
    #    uuid generation in PostgreSQL requires pgcrypto / gen_random_uuid()
    #    which may not be available; Python's uuid module is always present.
    #
    #    Safety contract:
    #      - Only rows where filming_guide is a non-empty JSONB array are touched.
    #      - Non-dict elements inside the array are silently skipped.
    #      - Rows already having shot_id on every element are left unchanged.
    conn = op.get_bind()
    rows = conn.execute(
        text("SELECT id, filming_guide FROM plan_items WHERE jsonb_array_length(filming_guide) > 0")
    ).fetchall()

    for row in rows:
        item_id, guide = row[0], row[1]
        changed = False
        new_guide = []
        for shot in guide:
            if not isinstance(shot, dict):
                # Defensive: skip malformed entries unchanged.
                new_guide.append(shot)
                continue
            if "shot_id" not in shot:
                shot = {**shot, "shot_id": uuid.uuid4().hex}
                changed = True
            new_guide.append(shot)

        if changed:
            import json

            conn.execute(
                text("UPDATE plan_items SET filming_guide = CAST(:guide AS jsonb) WHERE id = :id"),
                {"guide": json.dumps(new_guide), "id": str(item_id)},
            )


def downgrade() -> None:
    op.drop_column("plan_items", "clip_assignments")
    # Note: we do NOT revert shot_id from filming_guide entries — the column
    # is additive and the extra key is harmless; reverting JSON-in-place is
    # error-prone and unnecessary for a downgrade.
