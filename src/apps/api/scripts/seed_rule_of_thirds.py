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
from datetime import UTC, datetime
from urllib.parse import urlparse

# Bootstrap imports when run directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models import VideoTemplate  # noqa: E402

# Pinned UUID — matches the convention from commit 2d7b6e8 ("pin template UUIDs so
# renames don't break re-seeds"). Lookup is by id, not name, so an admin renaming
# this template via the UI will not cause the next seed run to create a duplicate.
TEMPLATE_ID = "f92fdd13-ef13-4d46-b140-59a6cf37aa1e"
TEMPLATE_NAME = "The Rule of Thirds"
TEMPLATE_DESCRIPTION = (
    "Composition tutorial style — each clip shows a 3x3 grid overlay, "
    "inspired by @josh._.choi on TikTok."
)
# Reference video object key, relative to STORAGE_BUCKET. Resolves to the same
# location in dev and prod via the env-configured bucket. analysis_status='ready'
# means the pipeline skips re-analysis at job time, but the poster-JPEG generator
# (template_poster.py) reads this object to extract a thumbnail for the homepage
# tile, so the file MUST exist in the active bucket before the template is used.
REFERENCE_GCS_PATH = "templates/rule-of-thirds/reference.mp4"
SOURCE_URL = "https://www.tiktok.com/@josh._.choi/video/7526684527020838199"
# Audio track extracted from the TikTok reference. The pipeline mixes this into
# the final render via _mix_template_audio; without it, the output has only
# stitched clip audio (silent during interstitials).
TEMPLATE_AUDIO_GCS_PATH = "templates/rule-of-thirds/audio.m4a"


def build_recipe() -> dict:
    """Build the Rule of Thirds recipe.

    Structure:
      - Slot 1 (hook, 3s): Title card with "The Rule of / Thirds" (red) spans.
                           No grid (title focuses attention on text).
      - Slots 2-7 (2s each): Content slots, all with rule-of-thirds grid.
      - No interstitials — hard cuts match the reference video's rhythm.
    """
    slots = []

    # Slot 1: Hook with 3-line title that arrives together, then beats red.
    #
    # Match the reference TikTok where:
    #   t=0.4s — all 3 words fade in TOGETHER, all white
    #   t=1.4s — beat hits: grid red L blinks AND "Thirds" turns red
    #            (white "Thirds" overlay ends, red "Thirds" overlay begins)
    #   t=2.4s — second beat: grid red L blinks again, Thirds stays red
    #   t=3.0s — slot ends, cuts to slot 2
    #
    # The "Thirds" word uses two overlays at the same position with sequential
    # times: the swap creates the beat-synced color change without needing ASS
    # \t() time animation. The white overlay's end matches the red overlay's
    # start (1.4s) which also matches grid_highlight_windows[0][0] in slot 1.
    _HOOK_BASE = {
        "role": "hook",
        "position": "center",
        "effect": "fade-in",
        "font_family": "Playfair Display",
        "font_style": "serif",
        "end_s": 3.0,
    }
    slots.append({
        "position": 1,
        "target_duration_s": 3.0,
        "priority": 10,
        "slot_type": "hook",
        "transition_in": "hard-cut",
        "color_hint": "none",
        "speed_factor": 1.0,
        "energy": 6.0,
        # Slot 1 carries the rule-of-thirds grid AND the title — matches the
        # reference where the Big Ben subject sits at the top-left intersection
        # under a red L while "The / Rule of / Thirds" stacks in the middle row.
        # Highlight starts OFF so the opening reads as a clean white grid; it
        # blinks twice on music beats: once when "Thirds" appears (t=1.4s) and
        # once near the end of the slot (t=2.4s) to lead into slot 2's red L.
        "has_grid": True,
        "grid_color": "#FFFFFF",
        "grid_opacity": 0.95,
        "grid_thickness": 6,
        "grid_highlight_intersection": "top-left",
        "grid_highlight_color": "#E63946",
        "grid_highlight_windows": [[1.4, 1.7], [2.4, 2.7]],
        # Title sits inside the middle row (between ih/3 and 2*ih/3) so the
        # 3 lines stack within the central rule-of-thirds cell.
        # Sizes tuned against the reference TikTok where each line takes ~5%
        # of frame height (~95px line height ≈ 80px font on 1920px tall video).
        # "Thirds" gets a small bump for red-emphasis without blowing past the
        # middle-column width (~360px on 1080 frame).
        # Y positions stack the 3 lines symmetrically around y=0.50 (middle of
        # middle row) so the title is visually centered inside the central cell.
        "text_overlays": [
            {
                **_HOOK_BASE,
                "text": "The",
                "text_size_px": 80,
                "text_color": "#FFFFFF",
                "start_s": 0.4,
                "position_y_frac": 0.45,
            },
            {
                **_HOOK_BASE,
                "text": "Rule of",
                "text_size_px": 80,
                "text_color": "#FFFFFF",
                "start_s": 0.4,
                "position_y_frac": 0.50,
            },
            # "Thirds" — white phase: appears with the other two, ends at beat.
            {
                **_HOOK_BASE,
                "text": "Thirds",
                "text_size_px": 95,
                "text_color": "#FFFFFF",
                "start_s": 0.4,
                "end_s": 1.4,
                "position_y_frac": 0.56,
            },
            # "Thirds" — red phase: takes over at the beat (1.4s) and stays.
            # Same position so it visually replaces the white one.
            {
                **_HOOK_BASE,
                "text": "Thirds",
                "text_size_px": 95,
                "text_color": "#E63946",
                "start_s": 1.4,
                "position_y_frac": 0.56,
            },
        ],
    })

    # Slots 2-7: Content with white 3x3 grid overlay + per-intersection highlight.
    #
    # Each slot is 2s, hard cut at the beat. We rotate the highlighted corner
    # across the four rule-of-thirds intersections so the viewer's eye is led
    # around the frame from shot to shot — this matches the reference video's
    # "L"-shaped red lines pointing at where the subject sits in each shot.
    #
    # Highlight is a beat-flash, NOT sustained: the cut shows a clean white
    # grid for ~80ms (2-3 frames visible), then the red L lights up for ~350ms
    # on the beat, then returns to white. Matches the reference TikTok and
    # matches slot 1's blink pattern (grid_highlight_windows). A sustained
    # red feels like a heavy lower-third instead of a snap-on-the-beat accent.
    _INTERSECTION_ROTATION = [
        "bottom-right",  # slot 2
        "top-left",      # slot 3
        "bottom-left",   # slot 4
        "top-right",     # slot 5
        "bottom-right",  # slot 6
        "top-left",      # slot 7
    ]
    for idx, pos in enumerate(range(2, 8)):
        slots.append({
            "position": pos,
            "target_duration_s": 2.0,
            "priority": 5,
            # "broll" matches the SlotType literal in admin.py RecipeSlotSchema
            # (see seed_dimples_passport_brazil.py for the existing convention).
            "slot_type": "broll",
            "transition_in": "hard-cut",
            "color_hint": "none",
            "speed_factor": 1.0,
            "energy": 5.5,
            "has_grid": True,
            "grid_color": "#FFFFFF",
            # Measured against the TikTok reference: white lines are ~6px wide
            # and nearly solid (RGB ~252,255,255). Highlight (red) is rendered
            # at 3x this thickness in reframe.py so the red L visually
            # dominates the white grid at the ~3:1 ratio in the reference.
            "grid_opacity": 0.95,
            "grid_thickness": 6,
            "grid_highlight_intersection": _INTERSECTION_ROTATION[idx],
            # The default of "#D9435A" in RecipeSlotSchema differs from this
            # explicit "#E63946" — set it explicitly so admin-UI re-saves of
            # the recipe don't quietly desaturate the highlight.
            "grid_highlight_color": "#E63946",
            # Beat flash: cut starts COMPLETELY white, red arrives on the
            # NEXT beat (mid-slot at t=1.0s, matching the ~1s beat cadence
            # implied by slot 1's [1.4, 1.7] / [2.4, 2.7] pattern), then
            # back to white. The red on the cut beat would collide with the
            # visual scene change; landing it on the next sub-beat reads as
            # punctuation, not a smear over the new shot.
            #   0.0  →  1.0   pure white grid (clean cut, eye adjusts)
            #   1.0  →  1.3   red L flash (the beat accent)
            #   1.3  →  2.0   back to pure white
            "grid_highlight_windows": [[1.0, 1.3]],
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
        # Idempotent: lookup by pinned id, not name. An admin can rename the
        # template via the admin UI without causing the next seed run to create
        # a duplicate row.
        existing = await db.execute(
            select(VideoTemplate).where(VideoTemplate.id == TEMPLATE_ID)
        )
        row = existing.scalar_one_or_none()
        now = datetime.now(UTC)
        if row:
            row.recipe_cached = recipe
            row.recipe_cached_at = now
            row.analysis_status = "ready"
            row.description = TEMPLATE_DESCRIPTION
            row.source_url = SOURCE_URL
            row.required_clips_min = 6
            row.required_clips_max = 12
            # Only set audio_gcs_path if it isn't already populated. This lets
            # ops upload audio under a different path without the seed clobbering
            # it, and still backfills the canonical path for fresh installs.
            if not row.audio_gcs_path:
                row.audio_gcs_path = TEMPLATE_AUDIO_GCS_PATH
            print(f"Updated existing template: {row.id} ({TEMPLATE_NAME})")
        else:
            template = VideoTemplate(
                id=TEMPLATE_ID,
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
                audio_gcs_path=TEMPLATE_AUDIO_GCS_PATH,
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
