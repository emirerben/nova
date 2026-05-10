"""Seed the "Dimples Passport Travel Vlog" template into the local DB.

The template existed only as a DB row in prod (created via admin UI), with no
source of truth in the repo. This script captures the prod recipe shape so the
template can be reseeded into a fresh DB.

Recipe shape was reconstructed on 2026-05-09 from:
  - Public `/templates` endpoint (slot count, target durations, total duration).
  - Job assembly_plan from `/template-jobs/1c214b1b-3675-495f-95ef-31bc5744bd5d/status`
    (slot_type, energy, priority, transition_in, text_overlays).

Notable shape:
  - 17 slots, total_duration_s ≈ 20.72.
  - Slot 1 has target_duration_s=0.1 (3 frames at 30fps). Pathological but
    intentional in the prod recipe — the truncation guard in _join_or_concat
    handles it.
  - Hook text overlays on slots 4-6: "Welcome to" (slot 4, fade-in) →
    "PERU" (slot 5, font-cycle) → "Welcome to PERU" (slot 6, none). Slot 6's
    overlay is the joined text that matches what _collect_absolute_overlays
    produces after cross-slot merge — kept here so editor previews look right.
  - No interstitials — hard cuts throughout, except slot 6's dissolve.

Safety: prints the target DB host before writing. Pass `--yes` to skip the
confirmation prompt (required for non-interactive runs; fails otherwise so a
stale env file can't accidentally seed staging/prod).

Run: cd src/apps/api && .venv/bin/python scripts/seed_dimples_passport_brazil.py
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

# Pinned id so admin renames don't break re-seeds. The script is the
# source of truth for this row's identity; `name` is a display label that
# can drift via the admin UI.
TEMPLATE_ID = "7b4a98d5-cf76-4724-a870-e2388d590932"
TEMPLATE_NAME = "Dimples Passport Travel Vlog"
TEMPLATE_DESCRIPTION = (
    "Energetic travel-vlog hook — quick passport-stamp intro slots, then a "
    "fast-cut montage. Inspired by @dimplespassport on TikTok."
)
# Reference video lives at this GCS path in dev. Production may have a
# different bucket or path — check before running this seed against prod.
REFERENCE_GCS_PATH = "templates/dimplespassport-travel-vlog.mp4"

# User-facing input that drives the "Welcome to <X>" hook overlay. Key MUST
# be "location" — _resolve_user_subject() in template_orchestrate.py reads
# inputs.location to populate the placeholder substitution. max_length 30
# accommodates long country names ("Democratic Republic of the Congo" wraps).
REQUIRED_INPUTS = [
    {
        "key": "location",
        "label": "Where did you go?",
        "placeholder": "e.g. Tokyo, Brazil, Bali",
        "max_length": 30,
        "required": True,
    },
]


# Tuned values from the standalone tuning UI at
# src/apps/web/public/position-tool.html. Open that file in a browser to
# re-tune; the Output Values panel exports the same names below.
PERU_SIZE_PX = 265              # jumbo, position-tool.html:79 default
PERU_Y_FRAC = 0.45              # position-tool.html:114 default
PERU_COLOR = "#F4D03F"          # position-tool.html:15 (Montserrat 800 yellow)
WELCOME_SIZE_PX = 48            # small, position-tool.html:105 default
WELCOME_Y_FRAC = 0.4779         # position-tool.html:95 default
WELCOME_COLOR = "#FFFFFF"       # position-tool.html:23 (Playfair Display white)


def _hook_overlay(
    text: str,
    *,
    effect: str,
    start_s: float,
    end_s: float,
    text_size_px: int | None = None,
    text_color: str = "#FFFFFF",
    font_style: str = "sans",
    position_y_frac: float | None = None,
) -> dict:
    return {
        "role": "hook",
        "text": text,
        "start_s": start_s,
        "end_s": end_s,
        "effect": effect,
        "position": "center",
        "text_size": "medium",          # fallback when text_size_px is None
        "text_size_px": text_size_px,
        "font_style": font_style,
        "text_color": text_color,
        "position_y_frac": position_y_frac,
        "has_darkening": False,
        "has_narrowing": False,
        "end_s_override": None,
        "start_s_override": None,
        "font_cycle_accel_at_s": None,
    }


def build_recipe() -> dict:
    """Build the Dimples Passport Travel Vlog recipe (17 slots, ~20.72s total)."""
    # Slots 1-3: ultra-short opening hooks (0.1, 0.99, 0.9s).
    # Slots 4-5: title hooks with "Welcome to" / "PERU" overlays.
    # Slot 6: dissolve into broll, opens with the joined "Welcome to PERU" caption.
    # Slots 7-15: fast-cut broll.
    # Slots 16-17: outro tail.
    slots = [
        {
            "position": 1, "target_duration_s": 0.1, "priority": 7, "slot_type": "hook",
            "transition_in": "none", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
        {
            "position": 2, "target_duration_s": 0.99, "priority": 6, "slot_type": "hook",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
        {
            "position": 3, "target_duration_s": 0.9, "priority": 7, "slot_type": "hook",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
        {
            "position": 4, "target_duration_s": 3.5, "priority": 10, "slot_type": "hook",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 2.1,
            "text_overlays": [
                _hook_overlay(
                    "Welcome to",
                    effect="fade-in",
                    start_s=0.5,
                    end_s=2.33,
                    text_size_px=WELCOME_SIZE_PX,
                    text_color=WELCOME_COLOR,
                    font_style="serif",
                    position_y_frac=WELCOME_Y_FRAC,
                ),
            ],
        },
        {
            "position": 5, "target_duration_s": 2.73, "priority": 10, "slot_type": "hook",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 1.8,
            "text_overlays": [
                _hook_overlay(
                    "PERU",
                    effect="font-cycle",
                    start_s=0.0,
                    end_s=2.73,
                    text_size_px=PERU_SIZE_PX,
                    text_color=PERU_COLOR,
                    font_style="sans",
                    position_y_frac=PERU_Y_FRAC,
                ),
            ],
        },
        {
            "position": 6, "target_duration_s": 0.96, "priority": 9, "slot_type": "broll",
            "transition_in": "dissolve", "color_hint": "none", "speed_factor": 1.0,
            "energy": 6.34,
            "text_overlays": [
                # Slot 6 hosts the joined "Welcome to PERU" caption shown
                # post-dissolve. Styled like the PERU hook so the visual
                # transition into b-roll preserves the title's weight/color.
                _hook_overlay(
                    "Welcome to PERU",
                    effect="none",
                    start_s=0.0,
                    end_s=1.96,
                    text_size_px=PERU_SIZE_PX,
                    text_color=PERU_COLOR,
                    font_style="sans",
                    position_y_frac=PERU_Y_FRAC,
                ),
            ],
        },
        {
            "position": 7, "target_duration_s": 1.0, "priority": 8, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
        {
            "position": 8, "target_duration_s": 1.07, "priority": 8, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 4.8, "text_overlays": [],
        },
        {
            "position": 9, "target_duration_s": 1.03, "priority": 9, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 8.5, "text_overlays": [],
        },
        {
            "position": 10, "target_duration_s": 1.0, "priority": 8, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 5.3, "text_overlays": [],
        },
        {
            "position": 11, "target_duration_s": 1.13, "priority": 9, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
        {
            "position": 12, "target_duration_s": 0.9, "priority": 8, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 4.5, "text_overlays": [],
        },
        {
            "position": 13, "target_duration_s": 1.03, "priority": 9, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 10.0, "text_overlays": [],
        },
        {
            "position": 14, "target_duration_s": 1.07, "priority": 8, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
        {
            "position": 15, "target_duration_s": 0.96, "priority": 8, "slot_type": "outro",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 3.6, "text_overlays": [],
        },
        {
            "position": 16, "target_duration_s": 1.33, "priority": 8, "slot_type": "outro",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
        {
            "position": 17, "target_duration_s": 1.33, "priority": 8, "slot_type": "outro",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
    ]

    return {
        # Explicit template_kind so future migrations and the orchestrator's
        # routing code don't have to fall back to the default.
        "template_kind": "multi_clip_montage",
        "shot_count": len(slots),
        "total_duration_s": sum(s["target_duration_s"] for s in slots),
        "hook_duration_s": 8.22,  # slots 1-5
        "slots": slots,
        "copy_tone": "energetic",
        "caption_style": "none",
        "creative_direction": (
            "fast-cut travel vlog with passport-stamp intro and Welcome-to-X reveal"
        ),
        "transition_style": "hard cuts with one dissolve into the body",
        "color_grade": "none",
        "pacing_style": "fast",
        "sync_style": "cut-on-beat",
        "interstitials": [],
    }


async def seed() -> None:
    recipe = build_recipe()
    async with AsyncSessionLocal() as db:
        existing = await db.execute(
            select(VideoTemplate).where(VideoTemplate.id == TEMPLATE_ID)
        )
        row = existing.scalars().first()
        now = datetime.now(UTC)
        if row:
            row.recipe_cached = recipe
            row.recipe_cached_at = now
            row.analysis_status = "ready"
            row.description = TEMPLATE_DESCRIPTION
            row.required_clips_min = 5
            row.required_clips_max = 20
            row.required_inputs = REQUIRED_INPUTS
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
                required_clips_min=5,
                required_clips_max=20,
                required_inputs=REQUIRED_INPUTS,
                published_at=now,
            )
            db.add(template)
            await db.flush()
            print(f"Created template: {template.id} ({TEMPLATE_NAME})")
        await db.commit()


def _confirm_target_db() -> None:
    db_url = os.environ.get("DATABASE_URL", "")
    parsed = urlparse(db_url)
    display = f"{parsed.hostname or '?'}:{parsed.port or '?'}{parsed.path or ''}"
    print(f"Target DB: {display}")
    if "--yes" in sys.argv:
        return
    if not sys.stdin.isatty():
        print("ERROR: non-interactive run requires --yes flag.", file=sys.stderr)
        sys.exit(2)
    answer = input("Proceed? [y/N]: ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)


if __name__ == "__main__":
    _confirm_target_db()
    asyncio.run(seed())
