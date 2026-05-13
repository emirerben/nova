"""Add the Morocco-style intro overlays to the Waka Waka template recipe.

The original Morocco template footage had "This / is / AFRICA" titles burned
into the locked hook slots (1 and 2) and the slot-3 broll. PR #114 unlocked
those slots so user clips would open the video, but the intro titles
disappeared because they lived in the now-replaced footage, not in
`recipe_cached.slots[*].text_overlays`. This script adds them as explicit
overlays so they render on top of whatever user clip fills the opening beats.

Recipe edits:

  1. slots[0] (position 1, hook, ~1.3s): "This"   white slide-up, top-left
  2. slots[1] (position 2, hook, ~1.2s): "is"     white slide-up, top-right
  3. slots[2] (position 3, broll, ~2.4s): "AFRICA" yellow font-cycle, center

Beat sync is automatic because slot boundaries already match the recipe's
`beat_timestamps_s` (slot 1 sonu = beat 2 at 1.3s; slot 2 sonu = beat 3 at
2.5s). Overlay `end_s` is bound to each slot's `target_duration_s`, so beat
drift after reanalysis stays in sync.

Idempotency: each slot is only written if its `text_overlays` array is
empty OR contains no overlay with the matching `sample_text`. A re-run on a
fully backfilled recipe is a no-op.

Position validation: aborts if `slots[i].position != i + 1` for i in 0..2.
Recipes whose slot order has been hand-shuffled need manual review first.

Default behavior is dry-run (inspect only). Pass `--apply` to write a new
TemplateRecipeVersion (trigger='manual_edit') and update recipe_cached.

Run:
    cd src/apps/api
    .venv/bin/python scripts/add_waka_waka_intro_overlays.py            # dry run
    .venv/bin/python scripts/add_waka_waka_intro_overlays.py --apply    # write

Against prod (Fly.io):
    fly ssh console -a nova-video
    cd /app && python scripts/add_waka_waka_intro_overlays.py --apply --yes
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
from datetime import UTC, datetime
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Same lookup as backfill_waka_waka_location.py — handles both naming spellings
# the prod template has used.
TEMPLATE_NAME_PATTERNS = ("%morocco%", "%waka%")

# Each intro overlay is keyed by its target slot index (0-based) and the
# expected `slot.position` (1-based) for the position guard. Effect, color,
# size, and placement come from a frame-by-frame analysis of morocco.mp4 —
# "This" sits in the upper-left, "is" in the upper-right staggered slightly
# lower, and "AFRICA" fills the center in maize/gold with font-cycle.
# Every intro overlay sets `subject_substitute: False` so the resolver in
# template_orchestrate._resolve_overlay_text returns the literal sample_text
# instead of substituting the user's location input. Without this flag,
# "This" gets rewritten to "Morocco" (title-case heuristic) and "AFRICA"
# gets rewritten to "MOROCCO" (all-caps heuristic). The styling path in
# _collect_absolute_overlays still classifies AFRICA as a "subject" label
# (font-cycle/accel@8s/maize) — the opt-out is text-only, not classification.
INTRO_OVERLAYS: list[dict] = [
    {
        "_slot_index": 0,
        "_expected_position": 1,
        "role": "label",
        "text": "",
        "sample_text": "This",
        "subject_substitute": False,
        "effect": "slide-up",
        "start_s": 0.0,
        # end_s set at write time from slot.target_duration_s.
        "position": "top",
        "position_x_frac": 0.25,
        "position_y_frac": 0.30,
        "text_size": "xxlarge",
        "text_size_px": 140,
        "font_style": "sans",
        "font_family": "Montserrat",
        "text_color": "#FFFFFF",
        "stroke_width": 0,
        "has_darkening": False,
    },
    {
        "_slot_index": 1,
        "_expected_position": 2,
        "role": "label",
        "text": "",
        "sample_text": "is",
        # Lowercase "is" does NOT match the placeholder heuristic, but we
        # set the flag for consistency and as a future-proofing assertion.
        "subject_substitute": False,
        "effect": "slide-up",
        "start_s": 0.0,
        "position": "top",
        "position_x_frac": 0.70,
        "position_y_frac": 0.35,
        "text_size": "xxlarge",
        "text_size_px": 140,
        "font_style": "sans",
        "font_family": "Montserrat",
        "text_color": "#FFFFFF",
        "stroke_width": 0,
        "has_darkening": False,
    },
    {
        "_slot_index": 2,
        "_expected_position": 3,
        "role": "label",
        "text": "",
        "sample_text": "AFRICA",
        # All-caps sample_text triggers _is_subject_placeholder=True in
        # _collect_absolute_overlays, which adds font_cycle_accel_at_s=8.0
        # from _LABEL_CONFIG["subject"]. That gives the font-cycle the same
        # "accelerate over time" feel the morocco source uses. The
        # subject_substitute: False flag below blocks the TEXT-substitution
        # path in _resolve_overlay_text (which would otherwise rewrite
        # "AFRICA" → "MOROCCO"), while leaving the classification path alone.
        "subject_substitute": False,
        "effect": "font-cycle",
        "start_s": 0.0,
        "position": "center",
        "position_x_frac": 0.50,
        "position_y_frac": 0.45,
        "text_size": "xxlarge",
        "text_size_px": 250,
        "font_style": "sans",
        "font_family": "Montserrat",
        # Maize/gold — matches _LABEL_CONFIG["subject"] default.
        "text_color": "#F4D03F",
        "stroke_width": 0,
        "has_darkening": False,
    },
]

_META_KEYS = {"_slot_index", "_expected_position"}


class PositionMismatchError(Exception):
    """Raised when slot order in recipe_cached doesn't match the expected
    1-based positions on slots 0..2. Better to abort than write to the wrong
    slot — operator should inspect the recipe first."""


async def find_template(db):
    from sqlalchemy import or_, select  # noqa: PLC0415

    from app.models import VideoTemplate  # noqa: PLC0415

    result = await db.execute(
        select(VideoTemplate).where(
            or_(*(VideoTemplate.name.ilike(p) for p in TEMPLATE_NAME_PATTERNS))
        )
    )
    rows = result.scalars().all()
    if not rows:
        patterns_repr = " OR ".join(f"ILIKE '{p}'" for p in TEMPLATE_NAME_PATTERNS)
        print(
            f"ERROR: no template matched name {patterns_repr}.",
            file=sys.stderr,
        )
        sys.exit(2)
    if len(rows) > 1:
        print(
            f"ERROR: {len(rows)} templates matched. Disambiguate before running:",
            file=sys.stderr,
        )
        for row in rows:
            print(f"  {row.id}  {row.name}", file=sys.stderr)
        sys.exit(2)
    return rows[0]


def _overlay_to_write(spec: dict, slot_duration_s: float) -> dict:
    """Strip the script-internal `_*` meta keys and bind end_s to the slot."""
    overlay = {k: v for k, v in spec.items() if k not in _META_KEYS}
    overlay["end_s"] = float(slot_duration_s)
    return overlay


def patch_recipe(recipe: dict) -> tuple[dict, list[tuple]]:
    """Return (patched_recipe, changes).

    `changes` is a list of tuples: ("add" | "upgrade", slot_idx, position, sample_text).

    Idempotency: an overlay whose `sample_text` already appears in the
    target slot's text_overlays contributes no "add", but is upgraded
    in-place when it lacks the required `subject_substitute: False` flag.
    The upgrade-in-place is what fixes templates that were patched by
    earlier versions of this script (which didn't write the flag) — without
    it, the resolver's casing heuristic rewrites "This"/"AFRICA" to the
    user's location and the rendered intro reads "Morocco" / "MOROCCO"
    instead of "This" / "AFRICA".

    Position guard: aborts via PositionMismatchError if slot.position
    doesn't match the spec.
    """
    patched = copy.deepcopy(recipe)
    changes: list[tuple] = []
    slots = patched.get("slots", [])

    for spec in INTRO_OVERLAYS:
        idx = spec["_slot_index"]
        expected_pos = spec["_expected_position"]
        sample = spec["sample_text"]

        if idx >= len(slots):
            raise PositionMismatchError(
                f"recipe has {len(slots)} slots, expected at least {idx + 1}"
            )

        slot = slots[idx]
        actual_pos = int(slot.get("position", idx + 1))
        if actual_pos != expected_pos:
            raise PositionMismatchError(
                f"slot index {idx} has position {actual_pos}, expected {expected_pos}"
            )

        existing = slot.get("text_overlays") or []
        match = next((ov for ov in existing if ov.get("sample_text") == sample), None)
        if match is not None:
            # Upgrade-in-place: existing intro overlay lacks the literal
            # opt-out flag. Patch it without disturbing other fields the
            # operator may have hand-tuned (positions, sizes, colors).
            if match.get("subject_substitute") is not False:
                match["subject_substitute"] = False
                changes.append(("upgrade", idx, actual_pos, sample))
            continue

        slot_duration = float(slot.get("target_duration_s", 1.0))
        overlay = _overlay_to_write(spec, slot_duration)

        if "text_overlays" not in slot or slot["text_overlays"] is None:
            slot["text_overlays"] = []
        slot["text_overlays"].append(overlay)
        changes.append(("add", idx, actual_pos, sample))

    return patched, changes


def _print_inspection(template, changes: list) -> None:
    print(f"Template: {template.id}  ({template.name})")
    print(f"analysis_status: {template.analysis_status}")
    if not changes:
        print(
            "recipe_cached: already backfilled (intro overlays present "
            "AND subject_substitute=False set)."
        )
        return
    adds = sum(1 for c in changes if c[0] == "add")
    upgrades = sum(1 for c in changes if c[0] == "upgrade")
    parts = []
    if adds:
        parts.append(f"add {adds} new overlay(s)")
    if upgrades:
        parts.append(f"upgrade {upgrades} existing overlay(s) (set subject_substitute=False)")
    print(f"\nWould {' AND '.join(parts)}:")
    for kind, slot_idx, position, sample in changes:
        spec = next(s for s in INTRO_OVERLAYS if s["_slot_index"] == slot_idx)
        print(
            f"  [{kind:>7}] slot {slot_idx:>2} (position {position}): "
            f"{sample!r}  effect={spec['effect']!r}  "
            f"x={spec['position_x_frac']}  y={spec['position_y_frac']}  "
            f"color={spec['text_color']}"
        )


async def run(apply_changes: bool) -> int:
    from app.database import AsyncSessionLocal  # noqa: PLC0415
    from app.models import TemplateRecipeVersion, VideoTemplate  # noqa: PLC0415

    async with AsyncSessionLocal() as db:
        template = await find_template(db)

        # Concurrent reanalysis guard — analyze_template_task overwrites
        # recipe_cached wholesale from fresh Gemini output, so any edits we
        # commit during a reanalyze window get clobbered.
        if apply_changes and template.analysis_status != "ready":
            print(
                f"ERROR: template.analysis_status is "
                f"{template.analysis_status!r}, not 'ready'. Reanalysis is "
                f"in flight (or failed). Refusing to apply — rerun after "
                f"status returns to 'ready' or admin clears the error.",
                file=sys.stderr,
            )
            return 3

        baseline_recipe_at = template.recipe_cached_at

        recipe = template.recipe_cached or {}
        try:
            patched, changes = patch_recipe(recipe)
        except PositionMismatchError as exc:
            print(f"ERROR: position guard tripped: {exc}", file=sys.stderr)
            print(
                "Recipe slot order doesn't match expected 1-2-3 layout. "
                "Inspect the recipe before re-running.",
                file=sys.stderr,
            )
            return 5

        if not changes:
            print(
                f"No changes — '{template.name}' is already backfilled "
                f"(all three intro overlays present)."
            )
            return 0

        _print_inspection(template, changes)

        if not apply_changes:
            print("\nDry run — pass --apply to write changes.")
            return 0

        version = TemplateRecipeVersion(
            template_id=template.id,
            recipe=patched,
            trigger="manual_edit",
        )
        db.add(version)
        template.recipe_cached = patched
        template.recipe_cached_at = datetime.now(UTC)

        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
            raise

        # Post-apply audit: fresh read confirms our edits stuck and no
        # concurrent reanalysis overwrote them.
        async with AsyncSessionLocal() as audit_db:
            fresh = await audit_db.get(VideoTemplate, template.id)
            if fresh is None:
                print(
                    "\nWARNING: post-apply audit could not re-read template row.",
                    file=sys.stderr,
                )
            else:
                drift = (
                    fresh.recipe_cached_at is not None
                    and baseline_recipe_at is not None
                    and fresh.recipe_cached_at < baseline_recipe_at
                )
                all_present = True
                for _kind, slot_idx, _pos, sample in changes:
                    ovs = (
                        fresh.recipe_cached["slots"][slot_idx].get("text_overlays")
                        or []
                    )
                    # Both add and upgrade paths must leave a matching
                    # overlay with subject_substitute=False on every slot.
                    matched = next(
                        (ov for ov in ovs if ov.get("sample_text") == sample),
                        None,
                    )
                    if matched is None or matched.get("subject_substitute") is not False:
                        all_present = False
                        break
                if drift or not all_present:
                    print(
                        f"\nERROR: post-apply audit found drift. "
                        f"all_present={all_present}, drift={drift}. Another "
                        f"process (likely analyze_template_task) overwrote "
                        f"our edits. RERUN this script once analysis_status "
                        f"is 'ready' again.",
                        file=sys.stderr,
                    )
                    return 4

        print(f"\nApplied. {len(changes)} intro overlay(s) written. Audit OK.")
        return 0


def _confirm_target_db(apply_changes: bool, yes_flag: bool) -> None:
    """Print the target DB and require confirmation before --apply.

    Mirrors backfill_waka_waka_location.py — protects against running
    against the wrong env via a stale DATABASE_URL.
    """
    db_url = os.environ.get("DATABASE_URL", "")
    parsed = urlparse(db_url)
    display = f"{parsed.hostname or '?'}:{parsed.port or '?'}{parsed.path or ''}"
    print(f"Target DB: {display}")
    if not apply_changes:
        return
    if yes_flag:
        return
    if not sys.stdin.isatty():
        print(
            "ERROR: non-interactive --apply requires --yes flag.",
            file=sys.stderr,
        )
        sys.exit(2)
    answer = input("Proceed with --apply? [y/N]: ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes (default is dry-run).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive DB confirmation (required for non-interactive --apply).",
    )
    args = parser.parse_args()

    _confirm_target_db(args.apply, args.yes)
    return asyncio.run(run(args.apply))


if __name__ == "__main__":
    sys.exit(main())
