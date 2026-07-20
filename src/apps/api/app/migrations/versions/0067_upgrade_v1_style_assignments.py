"""Upgrade v1-era cigdem creator-style assignments to v2.

The 2026-07-20 rollout decision is v2 for every user: the fleet default is
cigdem/v2 and an explicit ``creator_style_assignments`` row always beats
that default. Rows written during the v1 era (post-#664 testing, before the
admin assignment API existed) pin ``preset_version='v1'`` — those accounts
silently keep rendering WITHOUT chapter titles or contextual visuals while
the UI toggle suggests full Smart Captions. This migration lifts exactly
the ``cigdem/v1`` rows to ``v2``.

The affected-row count is printed so the Fly release log doubles as
verification of the production state (N>=1 confirms the stale-row root
cause of the 2026-07-20 "smart captions is not smart" report; N=0 means a
harmless no-op).

Revision ID: 0067
Revises: 0066
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The v1-era row was a manual SQL INSERT (no write surface existed) and
    # load_preset accepts both the canonical form ('v1') and the prefix form
    # ('cigdem-v1') — cover both.
    result = op.get_bind().execute(
        sa.text(
            "UPDATE creator_style_assignments "
            "SET preset_version = 'v2', updated_at = now() "
            "WHERE preset_id = 'cigdem' AND preset_version IN ('v1', 'cigdem-v1')"
        )
    )
    print(f"0067: upgraded {result.rowcount} cigdem/v1 assignment row(s) to v2")


def downgrade() -> None:
    # Best-effort inverse: restores every cigdem/v2 row to v1. Rows that were
    # ALREADY v2 before this migration (admin-API upserts) are indistinguishable
    # from upgraded ones, so a roundtrip may demote them — acceptable for a
    # rollback whose whole point is returning the fleet to v1 rendering.
    result = op.get_bind().execute(
        sa.text(
            "UPDATE creator_style_assignments "
            "SET preset_version = 'v1', updated_at = now() "
            "WHERE preset_id = 'cigdem' AND preset_version = 'v2'"
        )
    )
    print(f"0067: downgraded {result.rowcount} cigdem assignment row(s) to v1")
