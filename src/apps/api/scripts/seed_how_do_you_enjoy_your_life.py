"""Seed the "How do you enjoy your life?" template into the local DB.

Recipe shape: `template_kind="fixed_intro_dynamic_body"`. The template renders
~10.5s of fixed black-screen Q/A captions with a fixed voiceover, then drops
to a 19.5s segment auto-selected from the user's single uploaded video.

Reference: https://www.tiktok.com/@blessa079/video/7611396753882877202

Asset GCS paths must be uploaded separately (intro_voiceover.m4a and
music_30s_loop.m4a). See the asset-prep section in the design doc:
~/.gstack/projects/emirerben-nova/yasin-feat-template-how-do-you-enjoy-your-life-design-2026-05-04.md

Run: cd src/apps/api && .venv/bin/python scripts/seed_how_do_you_enjoy_your_life.py
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

TEMPLATE_NAME = "How do you enjoy your life?"
TEMPLATE_DESCRIPTION = (
    "Q/A interrogation intro on black screen with fixed voiceover, then "
    "drops to the best 19.5s segment of the user's uploaded video. "
    "Inspired by @blessa079 on TikTok."
)
SOURCE_URL = "https://www.tiktok.com/@blessa079/video/7611396753882877202"
INTRO_DURATION_S = 10.5
TOTAL_DURATION_S = 30.0
BODY_DURATION_S = TOTAL_DURATION_S - INTRO_DURATION_S  # 19.5

# Asset paths in GCS. Upload via:
#   gsutil cp /tmp/nova-template-assets/intro_voiceover.m4a \
#       gs://<bucket>/templates/how-do-you-enjoy-your-life/intro_voiceover.m4a
#   gsutil cp /tmp/nova-template-assets/music_30s_loop.m4a \
#       gs://<bucket>/templates/how-do-you-enjoy-your-life/music_30s_loop.m4a
# These are stored as object keys (not full gs:// URIs) for parity with
# `audio_gcs_path` on existing templates.
TEMPLATE_AUDIO_GCS_PATH = (
    "templates/how-do-you-enjoy-your-life/music_30s_loop.m4a"
)
TEMPLATE_VOICEOVER_GCS_PATH = (
    "templates/how-do-you-enjoy-your-life/intro_voiceover.m4a"
)
# Reference video. analysis_status="ready" skips Gemini re-analysis.
REFERENCE_GCS_PATH = (
    "templates/how-do-you-enjoy-your-life/reference.mp4"
)

# Caption timings extracted from frame analysis of the reference at 4fps.
# Each pair (Q, A) is shown briefly, leading up to the punchline question that
# triggers the body video drop. Timings have ~100ms tolerance — verified
# visually against frames 1-31 of the reference at 4fps.
INTRO_CAPTIONS = [
    {"text": "Do you smoke?",                "start_s": 0.0,  "end_s": 1.4},
    {"text": "No",                           "start_s": 1.5,  "end_s": 2.7},
    {"text": "Do you drink?",                "start_s": 3.0,  "end_s": 4.0},
    {"text": "Nah",                          "start_s": 4.2,  "end_s": 5.4},
    {"text": "Do you have a girlfriend?",    "start_s": 5.7,  "end_s": 7.1},
    {"text": "I don't have",                 "start_s": 7.3,  "end_s": 8.5},
    {"text": "Then...",                      "start_s": 8.7,  "end_s": 9.4},
    {"text": "How do you enjoy your life?",  "start_s": 9.6,  "end_s": 10.5},
]


def _validate_intro_captions(captions: list[dict], intro_duration_s: float) -> None:
    """Seed-time sanity check on caption timings.

    Inline asserts only — keeps validation close to the data and avoids a
    separate Pydantic module for what is ultimately 8 fixed strings.
    """
    assert captions, "INTRO_CAPTIONS must not be empty"
    sorted_caps = sorted(captions, key=lambda c: c["start_s"])
    for prev, cur in zip(sorted_caps, sorted_caps[1:]):
        assert cur["start_s"] >= prev["end_s"], (
            f"caption overlap: {prev['text']!r} (ends {prev['end_s']}) "
            f"→ {cur['text']!r} (starts {cur['start_s']})"
        )
    last = sorted_caps[-1]
    assert last["end_s"] <= intro_duration_s + 0.05, (
        f"last caption {last['text']!r} ends {last['end_s']}s "
        f"after intro_duration_s {intro_duration_s}s"
    )
    for cap in captions:
        assert cap["end_s"] > cap["start_s"], f"bad window: {cap}"
        assert cap["end_s"] - cap["start_s"] <= 5.0, f"caption too long: {cap}"
        assert 1 <= len(cap["text"]) <= 80, f"caption text length: {cap}"


def build_recipe() -> dict:
    """Build the fixed-intro-dynamic-body recipe."""
    _validate_intro_captions(INTRO_CAPTIONS, INTRO_DURATION_S)
    return {
        "template_kind": "fixed_intro_dynamic_body",
        "total_duration_s": TOTAL_DURATION_S,
        "intro_duration_s": INTRO_DURATION_S,
        "intro_captions": INTRO_CAPTIONS,
        "intro_caption_style": {
            "font_family": "DMSans-Bold",
            "font_size": 64,
            "color": "#FFFFFF",
            "shadow_color": "#000000",
            "shadow_blur": 8,
            "position": "center",
        },
        "intro_background_color": "#000000",
        "music_duck_db_intro": -12.0,
        "music_fadeup_ms": 200,
        "body": {
            "target_duration_s": BODY_DURATION_S,
            "tolerance_s": 1.5,
            "selection_strategy": "energy_hook",
            # Only start_s snaps to the nearest scene cut within tolerance.
            # end_s is fixed at start_s + target_duration_s so the total
            # output is always exactly TOTAL_DURATION_S. Bad end-cuts are
            # masked by the 0.5s music fade-out.
            "scene_snap_start": True,
        },
        # Compatibility shims: the admin EditorTab and other UI components
        # were built for the multi_clip_montage shape and call recipe.slots
        # / recipe.interstitials directly without null guards. Empty arrays
        # let those views render an "empty editor" instead of crashing. The
        # orchestrator routes on template_kind first, so neither field is
        # actually consumed for this template family.
        "slots": [],
        "interstitials": [],
    }


async def seed() -> None:
    recipe = build_recipe()
    async with AsyncSessionLocal() as db:
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
            row.required_clips_min = 1
            row.required_clips_max = 1
            if not row.audio_gcs_path:
                row.audio_gcs_path = TEMPLATE_AUDIO_GCS_PATH
            if not row.voiceover_gcs_path:
                row.voiceover_gcs_path = TEMPLATE_VOICEOVER_GCS_PATH
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
                required_clips_min=1,
                required_clips_max=1,
                published_at=now,
                audio_gcs_path=TEMPLATE_AUDIO_GCS_PATH,
                voiceover_gcs_path=TEMPLATE_VOICEOVER_GCS_PATH,
            )
            db.add(template)
            await db.flush()
            print(f"Created template: {template.id} ({TEMPLATE_NAME})")
        await db.commit()


def _confirm_target_db() -> None:
    """Print target DB host and require confirmation before seeding.

    Pass `--yes` for non-interactive runs.
    """
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
