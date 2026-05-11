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

# Beat-synced edit music that the slot durations were tuned for. When
# template.audio_gcs_path is set, the orchestrator's _mix_template_audio()
# (template_orchestrate.py:653) replaces the concatenated source-clip audio
# with this track in the final output. AAC 44.1kHz stereo at 192kbps so it
# matches BODY_SLOT_AUDIO_OUT_ARGS — no transcode glue at the mix step.
# Without this set, source-clip audio plays through and the template loses
# its beat-synced feel (the whole point of the format).
EDIT_MUSIC_GCS_PATH = "templates/dimplespassport-edit-music.m4a"

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
PERU_SIZE_PX = 170              # tuned to frame width via brazil.mp4 frame diff
                                # (2026-05-10): previous 265 overflowed the 1080px
                                # frame for every font in the cycle — the "L"
                                # clipped past the right edge. Reference glyphs
                                # span 35-55% frame width; 170px reproduces that
                                # range with the runtime PlayfairDisplay/Permanent
                                # Marker font set. position-tool.html's preview
                                # font has different metrics than the renderer,
                                # so the tool's default of 265 looked correct
                                # in-browser but rendered ~2x wider on output.
PERU_Y_FRAC = 0.45              # position-tool.html:114 default
PERU_COLOR = "#F4D03F"          # position-tool.html:15 (Montserrat 800 yellow)
WELCOME_SIZE_PX = 36            # tuned to REF welcome height via
                                # analyze_text_overlays.py (2026-05-10):
                                # REF welcome bbox height median 26px vs
                                # OURS 36px at WELCOME_SIZE_PX=48 → REF is
                                # using ~36px font. position-tool.html's
                                # default of 48 produces a rendered welcome
                                # ~35% bigger than the reference's tiny
                                # serif tag. Cap height ≈ 0.72 × font_size
                                # for Playfair Regular, so 36 → ~26px bbox
                                # matches REF exactly.
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
    font_cycle_accel_at_s: float | None = None,
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
        # When set, overrides the orchestrator's auto-injected accel point
        # (slot_end - animate_s). Lower value = more of the slot in fast-cycle
        # mode. Used here to make slot-5 font-cycle feel snappier on the beat.
        "font_cycle_accel_at_s": font_cycle_accel_at_s,
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
            # Slots 1-3: tuned to push slot 4 (welcome-alone) start to t=4.0s
            # and slot 5 (BRAZIL drop) start to t=5.1s, landing the title
            # within 15ms of beat 8 (5.085s — the music's bass drop before a
            # 3.4s silence). Beat analysis of templates/dimplespassport-edit-
            # music.m4a (2026-05-10) showed:
            #   beat 1: 1.42s  beat 5: 3.76s  beat 8: 5.085s ★ DROP
            #   beat 2: 1.81s  beat 6: 4.39s  ─── 3.4s silence ───
            #   beat 3: 2.42s  beat 7: 4.71s  beat 9: 8.50s
            # Earlier seed had slots 1-3 sum to 2.39s, placing BRAZIL at
            # 3.49s — between beats 4 (3.14) and 5 (3.76), in dead air. The
            # title missed the drop entirely, breaking the hook feel. New
            # values push the cuts to beat 1, beat 3, and beat 5 respectively.
            "position": 1, "target_duration_s": 1.0, "priority": 7, "slot_type": "hook",
            "transition_in": "none", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
        {
            # Cut at t=2.5s lands within 80ms of beat 3 (2.42s).
            "position": 2, "target_duration_s": 1.5, "priority": 6, "slot_type": "hook",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
        {
            # Cut at t=4.0s falls between beats 5 (3.76) and 6 (4.39).
            "position": 3, "target_duration_s": 1.5, "priority": 7, "slot_type": "hook",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
        {
            # Slot 4 — short "Welcome to" intro on a hook clip. Was 3.5s; cut
            # to 1.5s based on careful frame-by-frame analysis of the reference
            # video, which only holds "Welcome to" for ~0.7s before the
            # location title takes over. Trimmed further to 1.1s to compensate
            # for slot 1's +0.4s growth (0.1 → 0.5 above) so total template
            # duration stays close to the 21.4s music length.
            "position": 4, "target_duration_s": 1.1, "priority": 10, "slot_type": "hook",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 2.1,
            "text_overlays": [
                # Welcome appears at slot start (was 0.5s offset) so the
                # welcome-alone window is the full 1.1s of slot 4. Reference
                # holds welcome alone for ~1.05s before BRAZIL appears, so
                # 1.1s here matches almost exactly. effect="none" instead of
                # "fade-in" because _collect_absolute_overlays cross-slot-
                # merges this with slot 5's welcome (same text + same y),
                # and the merged overlay inherits the LATER slot's effect.
                # Keeping both effect="none" prevents the fade from being
                # silently stripped at merge time.
                _hook_overlay(
                    "Welcome to",
                    effect="none",
                    start_s=0.0,
                    end_s=1.1,
                    text_size_px=WELCOME_SIZE_PX,
                    text_color=WELCOME_COLOR,
                    font_style="serif",
                    position_y_frac=WELCOME_Y_FRAC,
                ),
            ],
        },
        {
            # Slot 5 — the title reveal moment. Reference holds "BRAZIL" on
            # screen for ~5.8 seconds with continuous font-cycling, then the
            # curtain closes over only the last ~1.6 seconds (28% of the title
            # phase). Previously this slot was 2.73s, of which 60% was curtain
            # — cycling had no room to read as rhythmic and the whole reveal
            # felt rushed. Extending to 5.5s gives the cycling 5+ seconds of
            # uncovered screen time and aligns the curtain-as-fraction with
            # the reference.
            "position": 5, "target_duration_s": 5.5, "priority": 10, "slot_type": "hook",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 1.8,
            "text_overlays": [
                # "Welcome to" co-renders with BRAZIL for the first 3.5s of
                # slot 5. Frame-by-frame of brazil.mp4 (2026-05-10) shows the
                # small white serif "Welcome to" visible inside/under the
                # BRAZIL letters from BRAZIL onset (~5.3s) through ~8.5s of
                # the reference — about 3.2s of co-render. The prior 0.4s
                # value made welcome disappear almost immediately, leaving
                # BRAZIL standing alone for the entire title phase. Cross-
                # slot merge with slot 4's welcome (gap < 2.0s threshold)
                # produces one continuous welcome span from slot 4 start
                # (absolute t=2.39s) through slot 5 t=3.5s (absolute
                # t=6.99s), matching the reference's welcome-under-BRAZIL
                # window of ~5.3s–8.5s.
                _hook_overlay(
                    "Welcome to",
                    effect="none",
                    start_s=0.0,
                    end_s=3.5,
                    text_size_px=WELCOME_SIZE_PX,
                    text_color=WELCOME_COLOR,
                    font_style="serif",
                    position_y_frac=WELCOME_Y_FRAC,
                ),
                # BRAZIL cycles slow-then-fast across slot 5, matching the
                # reference's accel ramp. Per-frame font-cycle analysis of
                # brazil.mp4 (analyze_brazil_animation.py, 2026-05-10) over
                # the full 6s title window showed the reference cycle runs:
                #   - 0.0s–2.8s into BRAZIL: ~0.132s interval (slow phase,
                #     matches FONT_CYCLE_INTERVAL_S=0.15)
                #   - 2.8s–end:              ~0.066s interval (fast phase,
                #     matches FONT_CYCLE_FAST_INTERVAL_S=0.07)
                # Earlier commit a5a11b4 set accel_at_s=0.0 based on a narrow
                # zoom clip (yazıörnek.mp4) that only captured the fast phase;
                # the wider sample showed the slow ramp the zoom missed.
                # accel_at_s=2.8 reproduces the reference timing exactly.
                # Frame budget: 2.8s / 0.15 + 2.7s / 0.07 = 18.6 + 38.6 ≈
                # 57 frames, well under MAX_FONT_CYCLE_FRAMES=100.
                # font_style="display" maps to PlayfairDisplay-Bold.ttf as
                # the settle/anchor font; the cycle rotates through 7
                # contrast fonts (Montserrat, Bodoni Moda, Fraunces,
                # Instrument Serif, Permanent Marker, Pacifico) — same
                # variety the reference uses.
                _hook_overlay(
                    "PERU",
                    effect="font-cycle",
                    start_s=0.0,
                    end_s=5.5,
                    text_size_px=PERU_SIZE_PX,
                    text_color=PERU_COLOR,
                    font_style="display",
                    position_y_frac=PERU_Y_FRAC,
                    font_cycle_accel_at_s=2.8,
                ),
            ],
        },
        {
            # Slot 6 was 0.96s; shrunk to 0.5s to keep total template duration
            # close to the music length (21.4s) after extending slot 5 by 2.77s
            # and shortening slot 4 by 2.0s.
            "position": 6, "target_duration_s": 0.5, "priority": 9, "slot_type": "broll",
            # Hard-cut after the curtain: the orchestrator force-overrides any
            # transition that follows an interstitial-bearing slot to "none"
            # (template_orchestrate.py:1677). Declaring "hard-cut" here keeps
            # the recipe honest about what actually renders. Reference video
            # also shows the bars reopen straight to b-roll with no fade.
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 6.34,
            # No joint caption after the curtain — the reference goes straight
            # to b-roll with no "Welcome to {LOCATION}" follow-up text.
            "text_overlays": [],
        },
        # Slots 7-17: b-roll trimmed proportionally (×0.864) from the prior
        # values to absorb the +1.61s budget that slots 1-3 took to land
        # BRAZIL on beat 8. Total recipe stays within 21.4s music length.
        {
            "position": 7, "target_duration_s": 0.86, "priority": 8, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
        {
            "position": 8, "target_duration_s": 0.93, "priority": 8, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 4.8, "text_overlays": [],
        },
        {
            "position": 9, "target_duration_s": 0.89, "priority": 9, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 8.5, "text_overlays": [],
        },
        {
            "position": 10, "target_duration_s": 0.86, "priority": 8, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 5.3, "text_overlays": [],
        },
        {
            "position": 11, "target_duration_s": 0.98, "priority": 9, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
        {
            "position": 12, "target_duration_s": 0.78, "priority": 8, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 4.5, "text_overlays": [],
        },
        {
            "position": 13, "target_duration_s": 0.89, "priority": 9, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 10.0, "text_overlays": [],
        },
        {
            "position": 14, "target_duration_s": 0.93, "priority": 8, "slot_type": "broll",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
        {
            "position": 15, "target_duration_s": 0.83, "priority": 8, "slot_type": "outro",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 3.6, "text_overlays": [],
        },
        {
            "position": 16, "target_duration_s": 1.15, "priority": 8, "slot_type": "outro",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
        {
            "position": 17, "target_duration_s": 1.15, "priority": 8, "slot_type": "outro",
            "transition_in": "hard-cut", "color_hint": "none", "speed_factor": 1.0,
            "energy": 0.0, "text_overlays": [],
        },
    ]

    return {
        # Explicit template_kind so future migrations and the orchestrator's
        # routing code don't have to fall back to the default.
        "template_kind": "multi_clip_montage",
        "shot_count": len(slots),
        # min_slots=17 (== shot_count) tells consolidate_slots to keep all
        # slots even when the user uploads fewer clips than slots. Without
        # this, the post-merge curtain validator drops slot 5's curtain-close
        # whenever consolidation runs (slot 5 dur 2.73s × 0.6 = 1.638s, below
        # the 4.0s _MIN_CURTAIN_ANIMATE_S floor), producing curtain-less output
        # for almost every job. The matcher rotates clips across slots instead
        # of merging — this is the documented behavior for snappy travel
        # templates (template_matcher.py:200).
        "min_slots": len(slots),
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
        "interstitials": [
            # Top+bottom black bars close over the slot-5 title reveal, framing
            # the location text before reopening to b-roll. Reference video shows
            # this animation 8s-11s; we let the orchestrator clamp animate_s to
            # _CURTAIN_MAX_RATIO (0.6) of slot-5 duration. hold_s=0 skips the
            # black-hold so the dissolve into slot 6 fires immediately.
            # Pre-burn keeps the PERU/{location} text visible through the close;
            # font_cycle_accel_at_s is auto-injected on the slot-5 overlay.
            {
                "after_slot": 5,
                "type": "curtain-close",
                # Matches the reference's curtain duration exactly. Well within
                # the orchestrator's slot_dur * _CURTAIN_MAX_RATIO clamp
                # (5.5 * 0.6 = 3.3s) and well above _MIN_CURTAIN_ANIMATE_S
                # (4.0s) for consolidate_slots — actually wait, 1.6 < 4.0
                # would still drop curtain in consolidation, but min_slots=17
                # below skips consolidation entirely so it's safe.
                "animate_s": 1.6,
                "hold_s": 0.0,
                "hold_color": "#000000",
            },
        ],
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
            row.audio_gcs_path = EDIT_MUSIC_GCS_PATH
            print(f"Updated existing template: {row.id} ({TEMPLATE_NAME})")
        else:
            template = VideoTemplate(
                id=TEMPLATE_ID,
                name=TEMPLATE_NAME,
                gcs_path=REFERENCE_GCS_PATH,
                audio_gcs_path=EDIT_MUSIC_GCS_PATH,
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
