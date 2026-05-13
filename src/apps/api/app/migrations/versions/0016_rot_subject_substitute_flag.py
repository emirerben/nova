"""Patch Rule of Thirds cached recipe to add subject_substitute=False.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-13

Background: job a1091488-09f6-4ce0-b92e-b1cc52695c9c rendered "pilot in cockpit"
(Gemini's per-clip detected_subject from a cockpit clip) in place of literal
"The"/"Thirds" because the hook overlays lacked the `subject_substitute: False`
opt-out flag and the orchestrator's casing heuristic substituted the template
text with the consensus Gemini subject. The seed script was updated in this PR
but `VideoTemplate.recipe_cached` (JSONB) is the source of truth at job-render
time — it does not refresh from seed scripts automatically.

This migration patches the cached recipe in place for the Rule of Thirds
template only (id f92fdd13-ef13-4d46-b140-59a6cf37aa1e). It walks every
text_overlay in the cached recipe and, for any overlay whose `role == "hook"`
that lacks the flag, sets `subject_substitute = False`.

Zero-downtime safe:
- Idempotent: re-running does nothing (the flag is already False on second pass).
- Scoped: only touches one row (the RoT template) and only adds a key that
  the renderer already handles.
- Reversible: downgrade removes the flag from those same overlays. Pipeline
  fix (_consensus_subject deletion) is the load-bearing protection; this
  flag is defense-in-depth for any future caller that supplies user_subject.

Pairs with the pipeline-level changes in template_orchestrate.py that closed
the `_consensus_subject` and `clip_meta.hook_text` fallback paths.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

# Pinned UUID from scripts/seed_rule_of_thirds.py:32. Matching this single id
# keeps the migration surgical — no other template's recipe gets touched.
ROT_TEMPLATE_ID = "f92fdd13-ef13-4d46-b140-59a6cf37aa1e"


def _patch_recipe(recipe: dict, *, add_flag: bool) -> tuple[dict, int]:
    """Walk recipe.slots[*].text_overlays and set/unset subject_substitute on
    every hook-role overlay.

    add_flag=True  → upgrade: add `subject_substitute: False` to hook overlays
                              that don't already declare it.
    add_flag=False → downgrade: remove `subject_substitute` from hook overlays
                                where we set it to False (don't touch explicit
                                True values).

    Returns (patched_recipe, overlays_changed).
    """
    if not isinstance(recipe, dict):
        return recipe, 0
    slots = recipe.get("slots") or []
    if not isinstance(slots, list):
        return recipe, 0
    changed = 0
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        overlays = slot.get("text_overlays") or []
        if not isinstance(overlays, list):
            continue
        for overlay in overlays:
            if not isinstance(overlay, dict):
                continue
            if overlay.get("role") != "hook":
                continue
            if add_flag:
                if "subject_substitute" not in overlay:
                    overlay["subject_substitute"] = False
                    changed += 1
            else:
                if overlay.get("subject_substitute") is False:
                    del overlay["subject_substitute"]
                    changed += 1
    return recipe, changed


def _update_recipe(conn, recipe: dict) -> None:
    """Write a recipe dict back to the RoT template row, casting to JSONB
    via the bound parameter."""
    conn.execute(
        sa.text(
            "UPDATE video_templates "
            "SET recipe_cached = CAST(:recipe AS JSONB), recipe_cached_at = NOW() "
            "WHERE id = :id"
        ),
        {"recipe": json.dumps(recipe), "id": ROT_TEMPLATE_ID},
    )


def upgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT recipe_cached FROM video_templates WHERE id = :id"),
        {"id": ROT_TEMPLATE_ID},
    ).fetchone()
    if row is None or row[0] is None:
        # Template not seeded in this environment (fresh CI DB, dev clone
        # without seed run, etc.). Nothing to patch.
        return
    recipe = dict(row[0]) if not isinstance(row[0], dict) else row[0]
    patched, changed = _patch_recipe(recipe, add_flag=True)
    if changed:
        _update_recipe(conn, patched)


def downgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT recipe_cached FROM video_templates WHERE id = :id"),
        {"id": ROT_TEMPLATE_ID},
    ).fetchone()
    if row is None or row[0] is None:
        return
    recipe = dict(row[0]) if not isinstance(row[0], dict) else row[0]
    patched, changed = _patch_recipe(recipe, add_flag=False)
    if changed:
        _update_recipe(conn, patched)
