"""Set freeze_layout=true on Impressing Myself pop-in overlays.

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-14

Companion to PR #138. The renderer's `_write_animated_ass` gates the
pre-wrap + \\q2 path that fixes pop-in scale-rewrap jitter on a per-overlay
`freeze_layout` flag. The flag must exist in the prod `recipe_cached` JSON
for the deployed renderer to apply the fix.

Scoped to template_id 936e9558-248f-49be-b857-2b9a193522c6 (Impressing
Myself) only — both of its pop-in overlays gain `freeze_layout: true`.
Other templates' recipes are untouched. Idempotent: re-running is a no-op
because the slots that would be modified already have the flag set.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

IMPRESSING_MYSELF_ID = "936e9558-248f-49be-b857-2b9a193522c6"


def _set_freeze_layout(recipe: dict, value: bool) -> dict:
    """Return a copy of `recipe` with freeze_layout set on every pop-in/bounce overlay."""
    new_recipe = json.loads(json.dumps(recipe))  # deep copy via JSON round-trip
    for slot in new_recipe.get("slots", []):
        for overlay in slot.get("text_overlays", []):
            if overlay.get("effect") in ("pop-in", "bounce"):
                overlay["freeze_layout"] = value
    return new_recipe


def upgrade() -> None:
    bind = op.get_bind()
    result = bind.execute(
        sa.text(
            "SELECT recipe_cached FROM video_templates WHERE id = :tid"
        ),
        {"tid": IMPRESSING_MYSELF_ID},
    ).fetchone()
    if result is None or result[0] is None:
        # Template row missing in this environment (e.g. fresh dev DB) — skip.
        return
    recipe = result[0]
    if not isinstance(recipe, dict):
        return
    updated = _set_freeze_layout(recipe, True)
    if updated == recipe:
        return  # already migrated — idempotent no-op
    bind.execute(
        sa.text(
            "UPDATE video_templates SET recipe_cached = :recipe, "
            "recipe_cached_at = NOW() WHERE id = :tid"
        ),
        {"recipe": json.dumps(updated), "tid": IMPRESSING_MYSELF_ID},
    )


def downgrade() -> None:
    bind = op.get_bind()
    result = bind.execute(
        sa.text(
            "SELECT recipe_cached FROM video_templates WHERE id = :tid"
        ),
        {"tid": IMPRESSING_MYSELF_ID},
    ).fetchone()
    if result is None or result[0] is None:
        return
    recipe = result[0]
    if not isinstance(recipe, dict):
        return
    updated = _set_freeze_layout(recipe, False)
    bind.execute(
        sa.text(
            "UPDATE video_templates SET recipe_cached = :recipe, "
            "recipe_cached_at = NOW() WHERE id = :tid"
        ),
        {"recipe": json.dumps(updated), "tid": IMPRESSING_MYSELF_ID},
    )
