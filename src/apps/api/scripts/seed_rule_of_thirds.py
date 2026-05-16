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

    # Slot 1: Hook with a 3-line title stacked inside the center cell of the
    # rule-of-thirds grid. "Thirds" snaps in red on the beat, line-synced
    # with the grid red L.
    #
    # Timeline:
    #   t=0.4s — "The" and "Rule of" both fade in white on lines 1 and 2
    #            (~500ms ASS fade-in inherited from _HOOK_BASE)
    #   t=0.9s — both white words fully solid
    #   t=1.4s — beat hits: grid red L snaps ON at top-left AND "Thirds" snaps
    #            in red on line 3 — both events on the same frame, NO fade-in
    #            on "Thirds" so its appearance locks to the line snap
    #   t=1.7s — red L snaps off; "Thirds" stays red
    #   t=2.4s — second beat: red L snaps on again; all 3 texts unchanged
    #   t=2.7s — red L snaps off; all 3 texts unchanged
    #   t=3.0s — slot ends, hard cut to slot 2; all 3 texts disappear on the
    #            same frame (ASS \fad has 0ms fade-out)
    #
    # Why "Thirds" uses effect="none": the inherited "fade-in" effect from
    # _HOOK_BASE renders as ASS \fad(500,0), which delays full opacity until
    # 1.9s — visibly behind the grid red L that snaps on instantly at 1.4s via
    # FFmpeg between(t,1.4,1.7). Setting effect="none" routes the overlay
    # through the static branch in text_overlay.py (any value outside
    # ASS_ANIMATED_EFFECTS falls through to the no-animation tag path), making
    # the text appear on the exact frame where t=start_s.
    #
    # Why 3 separate single-word overlays instead of "The Rule of" + "Thirds":
    # the joined phrase "The Rule of" at 95px Playfair is ~440px wide and
    # overflows the center cell of the rule-of-thirds grid (354px inside-width
    # between the 6px-thick vertical lines at x=iw/3 and x=2*iw/3). Splitting
    # to one word per line gives each line a width that fits — "Rule of" (the
    # widest white word) measures ~315px at 95px, with ~20px margin per side.
    #
    # subject_substitute=False (inherited from _HOOK_BASE) opts the literal
    # title words out of the placeholder-casing heuristic in
    # template_orchestrate._is_subject_placeholder (PR #125). Without it,
    # "The" and "Thirds" (single title-cased words) get re-classified as
    # variable subject tokens and replaced with clip_meta.hook_text ("pilot in
    # cockpit"), losing the recipe's color in the process (regression fixed
    # in PR #133).
    _HOOK_BASE = {
        "role": "hook",
        "position": "center",
        "effect": "fade-in",
        "font_family": "Playfair Display",
        "font_style": "serif",
        "end_s": 3.0,
        "subject_substitute": False,
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
        # under a red L while "The" / "Rule of" / "Thirds" stack inside the
        # center cell of the grid. Highlight starts OFF so the opening reads as
        # a clean white grid; it blinks twice on music beats: once when
        # "Thirds" appears (t=1.4s) and once near the end of the slot (t=2.4s)
        # to lead into slot 2's red L.
        "has_grid": True,
        "grid_color": "#FFFFFF",
        "grid_opacity": 0.95,
        "grid_thickness": 6,
        "grid_highlight_intersection": "top-left",
        "grid_highlight_color": "#E63946",
        "grid_highlight_windows": [[1.4, 1.7], [2.4, 2.7]],
        # 3-line title — each word on its own line, all three stacked inside
        # the center cell of the rule-of-thirds grid.
        #
        # `position_y_frac` is the VERTICAL CENTER of the rendered text
        # (text_overlay.py: `block_top = CANVAS_H * y_frac - block_h / 2`).
        # Center cell bounds on a 1080x1920 frame: y=640 (ih/3) to y=1280
        # (2*ih/3), height 640px. With 95px Playfair lines spread evenly:
        #   y_frac 0.43 -> center y=826 (line 1: "The")
        #   y_frac 0.50 -> center y=960 (line 2: "Rule of", dead center)
        #   y_frac 0.57 -> center y=1094 (line 3: "Thirds", red)
        # 134px between centers, ~39px gap between text blocks. Top text at
        # y=779 (139px below the y=640 top grid line). Bottom text ending at
        # y=1141 (139px above the y=1280 bottom grid line).
        #
        # All three overlays have distinct y_fracs, so _collect_absolute_overlays
        # treats them as separate screen slots — no dedup truncation.
        #
        # Sizes tuned against the reference: 95px Playfair on 1920px video.
        # Per-word horizontal width at 95px (measured from preview render):
        # "The" ~135px, "Rule of" ~315px, "Thirds" ~295px — all fit inside the
        # ~354px center-cell inside-width with margin. "Thirds" matches the
        # upper lines' size; emphasis comes from snap-on + red color, not
        # bigger type.
        "text_overlays": [
            {
                **_HOOK_BASE,
                "text": "The",
                "text_size_px": 95,
                "text_color": "#FFFFFF",
                "start_s": 0.4,
                "end_s": 3.0,
                "position_y_frac": 0.43,
            },
            {
                **_HOOK_BASE,
                "text": "Rule of",
                "text_size_px": 95,
                "text_color": "#FFFFFF",
                "start_s": 0.4,
                "end_s": 3.0,
                "position_y_frac": 0.50,
            },
            # "Thirds" snaps in on the beat at 1.4s — effect="none" overrides
            # the inherited _HOOK_BASE fade-in so the appearance frame matches
            # the grid red L's snap-on frame (grid_highlight_windows[0][0]).
            {
                **_HOOK_BASE,
                "effect": "none",
                "text": "Thirds",
                "text_size_px": 95,
                "text_color": "#E63946",
                "start_s": 1.4,
                "end_s": 3.0,
                "position_y_frac": 0.57,
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
