"""Seed the "Rule of Thirds" template into the local DB.

Creates a VideoTemplate row with a hand-crafted recipe that uses:
  - Rich inline text spans ("The Rule of" white + "Thirds" red) in the hook
  - Per-slot rule-of-thirds grid overlay (drawgrid) on content slots

Safety: prints the target DB host before writing. Pass `--yes` to skip the
confirmation prompt (required for non-interactive runs; fails otherwise so a
stale env file can't accidentally seed staging/prod).

Run: cd src/apps/api && .venv/bin/python scripts/seed_rule_of_thirds.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

# Bootstrap imports when run directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models import VideoTemplate  # noqa: E402

TEMPLATE_NAME = "The Rule of Thirds"
TEMPLATE_DESCRIPTION = (
    "Composition tutorial style — each clip shows a 3x3 grid overlay, "
    "inspired by @josh._.choi on TikTok."
)
# Reference video placeholder. In local dev this doesn't need to resolve to a real
# GCS object because analysis_status='ready' skips re-analysis. Replace with a real
# path if you want to re-run Gemini analysis.
REFERENCE_GCS_PATH = "gs://nova-videos-dev/templates/rule-of-thirds/reference.mp4"
SOURCE_URL = "https://www.tiktok.com/@josh._.choi/video/7526684527020838199"


def build_recipe() -> dict:
    """Build the Rule of Thirds recipe.

    Structure:
      - Slot 1 (hook, 3s): Title card with "The Rule of / Thirds" (red) spans.
                           No grid (title focuses attention on text).
      - Slots 2-7 (2s each): Content slots, all with rule-of-thirds grid.
      - No interstitials — hard cuts match the reference video's rhythm.
    """
    slots = []

    # Slot 1: Hook with title spans, no grid
    slots.append({
        "position": 1,
        "target_duration_s": 3.0,
        "priority": 10,
        "slot_type": "hook",
        "transition_in": "hard-cut",
        "color_hint": "none",
        "speed_factor": 1.0,
        "energy": 6.0,
        "has_grid": False,
        "text_overlays": [
            {
                "role": "hook",
                "text": "",
                "position": "center",
                "effect": "pop-in",
                "font_style": "serif",
                "text_size": "large",
                "text_color": "#FFFFFF",
                "start_s": 0.4,
                "end_s": 3.0,
                "position_y_frac": 0.42,
                "spans": [
                    {
                        "text": "The Rule of",
                        "font_family": "Playfair Display",
                        "text_color": "#FFFFFF",
                        "text_size": "large",
                    },
                    {
                        "text": "Thirds",
                        "font_family": "Playfair Display",
                        "text_color": "#E63946",
                        "text_size": "xlarge",
                    },
                ],
            },
        ],
    })

    # Slots 2-7: Content with white 3x3 grid overlay, 2s each (=12s total)
    for pos in range(2, 8):
        slots.append({
            "position": pos,
            "target_duration_s": 2.0,
            "priority": 5,
            "slot_type": "b-roll",
            "transition_in": "hard-cut",
            "color_hint": "none",
            "speed_factor": 1.0,
            "energy": 5.5,
            "has_grid": True,
            "grid_color": "#FFFFFF",
            "grid_opacity": 0.55,
            "grid_thickness": 3,
            "text_overlays": [],
        })

    return {
        "shot_count": len(slots),
        "total_duration_s": 3.0 + 2.0 * 6,  # 15s
        "hook_duration_s": 3.0,
        "slots": slots,
        "copy_tone": "educational",
        "caption_style": "none",
        "beat_timestamps_s": [3.0, 5.0, 7.0, 9.0, 11.0, 13.0],
        "creative_direction": "composition tutorial — highlight rule-of-thirds placement",
        "transition_style": "hard cuts",
        "color_grade": "none",
        "pacing_style": "steady",
        "sync_style": "cut-on-beat",
        "interstitials": [],
    }


async def seed() -> None:
    recipe = build_recipe()
    async with AsyncSessionLocal() as db:
        # Idempotent: replace any existing row with same name.
        # Use .first() rather than .scalar_one_or_none() so an accidental
        # duplicate name (e.g., admin renamed another template to match)
        # doesn't crash the seed run with MultipleResultsFound.
        existing = await db.execute(
            select(VideoTemplate).where(VideoTemplate.name == TEMPLATE_NAME)
        )
        row = existing.scalars().first()
        now = datetime.now(UTC)
        if row:
            row.recipe_cached = recipe
            row.recipe_cached_at = now
            row.analysis_status = "ready"
            row.description = TEMPLATE_DESCRIPTION
            row.source_url = SOURCE_URL
            row.required_clips_min = 6
            row.required_clips_max = 12
            print(f"Updated existing template: {row.id} ({TEMPLATE_NAME})")
        else:
            template = VideoTemplate(
                id=str(uuid.uuid4()),
                name=TEMPLATE_NAME,
                gcs_path=REFERENCE_GCS_PATH,
                recipe_cached=recipe,
                recipe_cached_at=now,
                analysis_status="ready",
                description=TEMPLATE_DESCRIPTION,
                source_url=SOURCE_URL,
                required_clips_min=6,
                required_clips_max=12,
                published_at=now,
            )
            db.add(template)
            await db.flush()
            print(f"Created template: {template.id} ({TEMPLATE_NAME})")
        await db.commit()


def _confirm_target_db() -> None:
    """Print target DB host and require confirmation before seeding.

    Protects against running this script with a stale .env pointing at
    staging/prod by mistake. Pass `--yes` for non-interactive runs.
    """
    db_url = os.environ.get("DATABASE_URL", "")
    parsed = urlparse(db_url)
    display = f"{parsed.hostname or '?'}:{parsed.port or '?'}{parsed.path or ''}"
    print(f"Target DB: {display}")
    if "--yes" in sys.argv:
        return
    if not sys.stdin.isatty():
        # Non-interactive + no --yes: refuse rather than silently proceeding.
        print("ERROR: non-interactive run requires --yes flag.", file=sys.stderr)
        sys.exit(2)
    answer = input("Proceed? [y/N]: ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)


if __name__ == "__main__":
    _confirm_target_db()
    asyncio.run(seed())
