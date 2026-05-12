"""Backfill the Waka Waka / Morocco template so the outro becomes user-driven.

The template's outro reaction overlay currently burns the literal phrase
"shukran Morocco!" into every video. This script:

  1. Sets `required_inputs = [{"key": "location", ...}]` so the upload UI
     prompts for a location — same UX as Dimples Passport / "That one trip
     to..." (label "Where was this trip?").

  2. Tags the outro overlay in `recipe_cached.slots[*].text_overlays` with
     `subject_template = "shukran {subject}!"` so the renderer wraps the
     user's input into the bounce-effect outro. Consumed by
     `_resolve_overlay_text` in `app/tasks/template_orchestrate.py`.

  3. Rewrites the overlay's `sample_text` from "shukran Morocco!" to
     "shukran Africa!" so the blank-input fallback renders the Waka Waka
     song-lyric reference instead of a stale country literal. The
     subject_template branch of _resolve_overlay_text returns
     `overlay.get("text") or sample` when subject is empty — `text` is "",
     so it falls through to the new sample_text.

Matching is case-insensitive on the overlay's `sample_text` (or `text` if
sample_text is empty), keyed on a single literal:

    "shukran morocco!" → tag with subject_template + rewrite sample_text
    "shukran africa!"  → already partway through backfill: just ensure
                         the subject_template tag is present

If neither literal is found anywhere in the recipe, the script logs a
warning and still updates `required_inputs` so the upload UI can collect
the location even if the recipe was hand-edited per PR #111's plan.

Default behavior is dry-run (inspect only). Pass `--apply` to write a new
TemplateRecipeVersion (trigger='manual_edit') and update recipe_cached.
Idempotent: rerunning after a successful apply is a no-op.

Run:
    cd src/apps/api
    .venv/bin/python scripts/backfill_waka_waka_location.py            # dry run
    .venv/bin/python scripts/backfill_waka_waka_location.py --apply    # write

Against prod (Fly.io):
    fly ssh console -a nova-video
    cd /app && python scripts/backfill_waka_waka_location.py --apply --yes
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

# Match anything containing "morocco" or "waka" case-insensitively. The prod
# template name has historically used both spellings (the song is "Waka Waka",
# the destination is "Morocco"). ilike + wildcard handles either.
TEMPLATE_NAME_PATTERN = "%morocco%"

REQUIRED_INPUTS = [
    {
        "key": "location",
        "label": "Where was this trip?",
        "placeholder": "Bali, Tokyo, Morocco…",
        "max_length": 30,
        "required": False,
    },
]

OUTRO_TEMPLATE = "shukran {subject}!"
# The new canonical sample_text — falls back here when the user leaves the
# location input blank. Was "shukran Morocco!" pre-backfill.
NEW_SAMPLE_TEXT = "shukran Africa!"

# Case-insensitive literals that identify the outro overlay. Either the
# pre-backfill state ("shukran morocco!") or a partial / post-rename state
# ("shukran africa!" without the template tag) qualifies.
OUTRO_SAMPLES = {"shukran morocco!", "shukran africa!"}


async def find_template(db):
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import VideoTemplate  # noqa: PLC0415

    result = await db.execute(
        select(VideoTemplate).where(
            VideoTemplate.name.ilike(TEMPLATE_NAME_PATTERN)
        )
    )
    rows = result.scalars().all()
    if not rows:
        print(
            f"ERROR: no template matched name ILIKE '{TEMPLATE_NAME_PATTERN}'.",
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


def _overlay_sample(overlay: dict) -> str:
    return (overlay.get("sample_text") or overlay.get("text") or "").strip()


def patch_recipe(recipe: dict) -> tuple[dict, list[tuple]]:
    """Return (patched_recipe, changes).

    `changes` is a list of tuples:
        ("outro", slot_idx, ov_idx, old_sample, new_sample, template)

    An overlay that already carries the correct sample_text + subject_template
    contributes nothing — the script is idempotent.
    """
    patched = copy.deepcopy(recipe)
    changes: list[tuple] = []

    for slot_idx, slot in enumerate(patched.get("slots", [])):
        for ov_idx, overlay in enumerate(slot.get("text_overlays", [])):
            sample_raw = _overlay_sample(overlay)
            sample_lc = sample_raw.lower()
            if sample_lc not in OUTRO_SAMPLES:
                continue

            current_template = overlay.get("subject_template")
            current_sample = overlay.get("sample_text", "")
            if (
                current_template == OUTRO_TEMPLATE
                and current_sample == NEW_SAMPLE_TEXT
            ):
                # Fully backfilled already.
                continue

            changes.append((
                "outro", slot_idx, ov_idx,
                current_sample, NEW_SAMPLE_TEXT, OUTRO_TEMPLATE,
            ))
            overlay["sample_text"] = NEW_SAMPLE_TEXT
            overlay["subject_template"] = OUTRO_TEMPLATE

    return patched, changes


def _print_inspection(template, changes: list) -> None:
    print(f"Template: {template.id}  ({template.name})")
    print(f"analysis_status: {template.analysis_status}")
    print(f"current required_inputs: {template.required_inputs!r}")
    if not changes:
        print("recipe_cached: outro overlay already tagged (or not present).")
        return
    print(f"\nWould tag {len(changes)} outro overlay(s):")
    for _kind, slot_idx, ov_idx, old_sample, new_sample, template_str in changes:
        print(
            f"  slot {slot_idx:>2} overlay {ov_idx:>2} "
            f"sample_text: {old_sample!r} → {new_sample!r}, "
            f"subject_template: {template_str!r}"
        )


async def run(apply_changes: bool) -> int:
    from app.database import AsyncSessionLocal  # noqa: PLC0415
    from app.models import TemplateRecipeVersion  # noqa: PLC0415

    async with AsyncSessionLocal() as db:
        template = await find_template(db)

        recipe = template.recipe_cached or {}
        patched, changes = patch_recipe(recipe)

        inputs_match = template.required_inputs == REQUIRED_INPUTS
        if not changes and inputs_match:
            print(
                f"No changes — '{template.name}' is already backfilled "
                f"(required_inputs set, outro overlay tagged)."
            )
            return 0

        _print_inspection(template, changes)
        if not inputs_match:
            print("\nWould set required_inputs to:")
            print(f"  {REQUIRED_INPUTS}")

        if not changes and not inputs_match:
            # Recipe lacks the outro overlay entirely (already hand-edited per
            # PR #111's plan, or in an unexpected shape). Still wire up the
            # input UI so users can submit a location — it just won't render
            # in this template until the overlay is re-added downstream.
            print(
                "\nWARNING: outro overlay not found. Will set required_inputs "
                "only; recipe_cached untouched.",
                file=sys.stderr,
            )

        if not apply_changes:
            print("\nDry run — pass --apply to write changes.")
            return 0

        if not inputs_match:
            template.required_inputs = REQUIRED_INPUTS
        if changes:
            version = TemplateRecipeVersion(
                template_id=template.id,
                recipe=patched,
                trigger="manual_edit",
            )
            db.add(version)
            template.recipe_cached = patched
            template.recipe_cached_at = datetime.now(UTC)

        await db.commit()
        print(
            f"\nApplied. required_inputs updated={not inputs_match}, "
            f"overlay tags written={len(changes)}."
        )
        return 0


def _confirm_target_db(apply_changes: bool, yes_flag: bool) -> None:
    """Print the target DB and require confirmation before --apply.

    Mirrors backfill_that_one_trip_to.py — protects against running against
    the wrong env via a stale DATABASE_URL.
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
