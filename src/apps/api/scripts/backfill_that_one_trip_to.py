"""Backfill the "That one trip to..." template so the city becomes user-driven.

The template currently burns the literal word "london" (rendered as two
staggered overlays "lon" + "don") into every video, regardless of the user's
actual destination. This script:

  1. Sets `required_inputs = [{"key": "location", ...}]` so the upload UI
     prompts for a city — same UX as Dimples Passport's "Where was this
     filmed?" input.

  2. Tags the fragment overlays in `recipe_cached.slots[*].text_overlays`
     with `subject_part` so the renderer knows to slice the user's input
     across them. The new field is consumed by `_resolve_overlay_text` in
     `app/tasks/template_orchestrate.py` (added 2026-05-09).

Matching is case-insensitive on `sample_text` (also `text` if `sample_text`
is empty), keyed on three literals:

    sample_text == "lon"     → subject_part = "first_half"
    sample_text == "don"     → subject_part = "second_half"
    sample_text == "london"  → subject_part = "full"

Default behavior is dry-run (inspect only). Pass `--apply` to write a new
TemplateRecipeVersion (trigger='manual_edit') and update recipe_cached.
Idempotent: rerunning after a successful apply is a no-op.

Run:
    cd src/apps/api
    .venv/bin/python scripts/backfill_that_one_trip_to.py            # dry run
    .venv/bin/python scripts/backfill_that_one_trip_to.py --apply    # write

Against prod (Fly.io):
    fly ssh console -a nova-video
    cd /app && python scripts/backfill_that_one_trip_to.py --apply --yes
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
from datetime import UTC, datetime
from urllib.parse import urlparse

# Bootstrap imports when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEMPLATE_NAME_PATTERN = "that one trip to%"

REQUIRED_INPUTS = [
    {
        "key": "location",
        "label": "Where was this trip?",
        "placeholder": "London, Tokyo, Paris…",
        "max_length": 30,
        "required": False,
    },
]

# Literal sample_text matches → subject_part value. Matching is
# case-insensitive on the overlay's sample_text (or `text` if sample_text is
# empty). Keep this map small and explicit so we don't accidentally tag an
# unrelated overlay.
FRAGMENT_MAP = {
    "lon": "first_half",
    "don": "second_half",
    "london": "full",
}

# Typewriter sentences: overlays whose `text` ends with " london" or " lon"
# (case-insensitive) get a subject_template that wraps the user's input. The
# suffix length determines subject_chars (None for full, 3 for the partial
# "lon" beat). Used for slots 0-5 of "That one trip to..." which type the
# sentence out word-by-word: ..., "that one trip to lon", "that one trip to
# london". Suffix matching is right-anchored so "London" in the middle of a
# longer sentence won't accidentally get tagged.
TYPEWRITER_SUFFIXES = [
    # (lowercase suffix, subject_chars). Order matters — longest first.
    ("london", None),
    ("lon", 3),
]


async def find_template(db):
    # Lazy imports: `app.config.settings` validates env on import, so unit
    # tests of pure helpers (patch_recipe, _overlay_sample) can import this
    # module without a DATABASE_URL set.
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


def _typewriter_match(sample: str) -> tuple[str, int | None] | None:
    """If `sample` ends with a typewriter suffix, return (prefix, subject_chars).

    "that one trip to london" → ("that one trip to ", None)
    "that one trip to lon"    → ("that one trip to ", 3)
    "london"                  → None  (handled by FRAGMENT_MAP, not typewriter)
    """
    lower = sample.lower()
    for suffix, chars in TYPEWRITER_SUFFIXES:
        # Must have a non-empty prefix — bare "london" or "lon" hits FRAGMENT_MAP.
        if lower.endswith(" " + suffix) and len(lower) > len(suffix) + 1:
            prefix = sample[: len(sample) - len(suffix)]
            return prefix, chars
    return None


def patch_recipe(recipe: dict) -> tuple[dict, list[tuple]]:
    """Return (patched_recipe, changes).

    `changes` is a list of tuples describing every tag that was applied or
    would be applied. Two shapes:

    - Fragment tag:  ("fragment", slot_idx, ov_idx, sample, old_part, new_part)
    - Typewriter:    ("typewriter", slot_idx, ov_idx, sample, template, chars)

    An overlay that already carries the correct tags contributes nothing.
    """
    patched = copy.deepcopy(recipe)
    changes: list[tuple] = []

    for slot_idx, slot in enumerate(patched.get("slots", [])):
        for ov_idx, overlay in enumerate(slot.get("text_overlays", [])):
            sample_raw = _overlay_sample(overlay)
            sample_lc = sample_raw.lower()

            # Path 1: bare-word fragment ("lon"/"don"/"london") → subject_part.
            target = FRAGMENT_MAP.get(sample_lc)
            if target is not None:
                if overlay.get("subject_part") != target:
                    changes.append((
                        "fragment", slot_idx, ov_idx, sample_raw,
                        overlay.get("subject_part"), target,
                    ))
                    overlay["subject_part"] = target
                continue

            # Path 2: typewriter sentence ("that one trip to lon"/"...london")
            # → subject_template + optional subject_chars.
            tw = _typewriter_match(sample_raw)
            if tw is not None:
                prefix, chars = tw
                template = prefix + "{subject}"
                if (
                    overlay.get("subject_template") != template
                    or overlay.get("subject_chars") != chars
                ):
                    changes.append((
                        "typewriter", slot_idx, ov_idx, sample_raw, template, chars,
                    ))
                    overlay["subject_template"] = template
                    overlay["subject_chars"] = chars

    return patched, changes


def _print_inspection(template, changes: list) -> None:
    print(f"Template: {template.id}  ({template.name})")
    print(f"analysis_status: {template.analysis_status}")
    print(f"current required_inputs: {template.required_inputs!r}")
    if not changes:
        print("recipe_cached: no overlay tags need updating (already backfilled).")
        return
    print(f"\nWould tag {len(changes)} overlay(s):")
    for change in changes:
        kind = change[0]
        if kind == "fragment":
            _, slot_idx, ov_idx, sample, old, new = change
            old_label = "—" if old is None else old
            print(
                f"  slot {slot_idx:>2} overlay {ov_idx:>2} "
                f"[fragment]   sample={sample!r:<28} subject_part: {old_label} → {new}"
            )
        elif kind == "typewriter":
            _, slot_idx, ov_idx, sample, template_str, chars = change
            chars_label = "full" if chars is None else f"first_{chars}"
            print(
                f"  slot {slot_idx:>2} overlay {ov_idx:>2} "
                f"[typewriter] sample={sample!r:<28} template={template_str!r} ({chars_label})"
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
                f"(required_inputs set, all fragment overlays tagged)."
            )
            return 0

        _print_inspection(template, changes)
        if not inputs_match:
            print("\nWould set required_inputs to:")
            print(f"  {REQUIRED_INPUTS}")

        if not apply_changes:
            print("\nDry run — pass --apply to write changes.")
            return 0

        # Apply
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

    Mirrors seed_dimples_passport_brazil.py — protects against running
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
