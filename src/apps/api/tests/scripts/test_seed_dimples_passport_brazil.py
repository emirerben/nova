"""Recipe contract tests for the Dimples Passport Brazil template seed.

The PERU/Welcome-to title hooks are pixel-tuned in
src/apps/web/public/position-tool.html — those values must round-trip into the
recipe so the admin editor preview matches what the renderer ships. A
regression here turns the title screen back into 90px white centered text and
silently breaks the template's signature look.
"""
import importlib.util
import os
import sys

import pytest

_SEED_PATH = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "seed_dimples_passport_brazil.py"
))
_spec = importlib.util.spec_from_file_location("seed_dimples_passport_brazil", _SEED_PATH)
_seed = importlib.util.module_from_spec(_spec)
sys.modules["seed_dimples_passport_brazil"] = _seed
_spec.loader.exec_module(_seed)
build_recipe = _seed.build_recipe


def _slot(recipe: dict, position: int) -> dict:
    for s in recipe["slots"]:
        if s["position"] == position:
            return s
    raise AssertionError(f"slot {position} missing from recipe")


def _only_overlay(slot: dict) -> dict:
    overlays = slot.get("text_overlays") or []
    assert len(overlays) == 1, (
        f"slot {slot['position']} expected exactly 1 overlay, got {len(overlays)}"
    )
    return overlays[0]


def _subject_overlay(slot: dict) -> dict:
    """Pick the location-title overlay from a slot that may have a co-rendered
    prefix. The subject overlay is the font-cycle one (the jumbo location
    title); the prefix is rendered as a separate static overlay alongside it."""
    overlays = slot.get("text_overlays") or []
    subject = [o for o in overlays if o.get("effect") == "font-cycle"]
    assert len(subject) == 1, (
        f"slot {slot['position']} expected exactly 1 font-cycle overlay "
        f"(the location title), got {len(subject)}"
    )
    return subject[0]


def _prefix_overlay(slot: dict) -> dict | None:
    """Pick the 'Welcome to' prefix overlay from a slot, or None if absent.
    Identified by white color + non-jumbo size + non-font-cycle effect."""
    overlays = slot.get("text_overlays") or []
    candidates = [
        o for o in overlays
        if o.get("text") == "Welcome to" and o.get("effect") != "font-cycle"
    ]
    return candidates[0] if candidates else None


class TestPeruHookSizing:
    """Slot 5 is the PERU font-cycle moment — must be the jumbo yellow heading."""

    def test_size_matches_seed_constant(self):
        recipe = build_recipe()
        overlay = _subject_overlay(_slot(recipe, 5))
        assert overlay["text"] == "PERU"
        assert overlay["text_size_px"] == _seed.PERU_SIZE_PX == 170, (
            "PERU drifted from the frame-fit size (170px). Reference brazil.mp4 "
            "shows BRAZIL glyphs at 35-55% frame width across the cycle; 170px "
            "reproduces that with the runtime font set. Higher values (e.g. the "
            "old 265) overflow the 1080px frame and clip the 'L'."
        )

    def test_fits_within_1080px_frame_width(self):
        """Direct frame-width sanity: with the widest brush font in the cycle
        (Permanent Marker), the rendered BRAZIL must stay inside 1080px. At
        265px font size the 'L' clipped off the right edge in every cycle
        frame of dimples-CONSTANT-CYCLE.mp4 (2026-05-10). 170 keeps it in."""
        overlay = _subject_overlay(_slot(build_recipe(), 5))
        assert overlay["text_size_px"] <= 180, (
            f"text_size_px={overlay['text_size_px']} likely overflows the "
            f"1080px frame for wide brush fonts (Permanent Marker, Pacifico) "
            f"in the cycle. Reference glyph widths cap at ~55% frame width "
            f"— stay at 170-180px."
        )

    def test_color_matches_montserrat_yellow(self):
        recipe = build_recipe()
        overlay = _subject_overlay(_slot(recipe, 5))
        assert overlay["text_color"].upper() == _seed.PERU_COLOR.upper() == "#F4D03F"

    def test_y_position_matches_tool(self):
        recipe = build_recipe()
        overlay = _subject_overlay(_slot(recipe, 5))
        assert overlay["position_y_frac"] == pytest.approx(_seed.PERU_Y_FRAC) == pytest.approx(0.45)

    def test_uses_display_font_for_bold_serif(self):
        """Reference renders the location title in a BOLD yellow serif
        (PlayfairDisplay-Bold.ttf). The font registry maps:
          font_style="display" → PlayfairDisplay-Bold.ttf  (weight 700) ← USE THIS
          font_style="serif"   → PlayfairDisplay-Regular.ttf (weight 400) ← TOO THIN
        At the 170px hook size the difference is still visible — Regular
        looks anemic and breaks the template's signature look."""
        recipe = build_recipe()
        overlay = _subject_overlay(_slot(recipe, 5))
        assert overlay["font_style"] == "display", (
            f"font_style={overlay['font_style']!r} resolves to a non-Bold "
            f"weight. Use 'display' to get PlayfairDisplay-Bold.ttf — the "
            f"Bold weight the reference video uses at 265px jumbo size."
        )

    def test_effect_is_font_cycle(self):
        recipe = build_recipe()
        overlay = _subject_overlay(_slot(recipe, 5))
        assert overlay["effect"] == "font-cycle"


class TestWelcomeToHookSizing:
    """Slot 4 is the small serif "Welcome to" sitting just below PERU."""

    def test_size_matches_reference_height(self):
        recipe = build_recipe()
        overlay = _only_overlay(_slot(recipe, 4))
        assert overlay["text"] == "Welcome to"
        assert overlay["text_size_px"] == _seed.WELCOME_SIZE_PX == 36, (
            "Welcome to drifted from REF-match size (36px). Reference welcome "
            "bbox height is 26px; at cap-height ratio ~0.72 that's a 36px font. "
            "The old 48px default produced welcome ~35% larger than reference."
        )

    def test_color_is_white(self):
        recipe = build_recipe()
        overlay = _only_overlay(_slot(recipe, 4))
        assert overlay["text_color"].upper() == _seed.WELCOME_COLOR.upper() == "#FFFFFF"

    def test_uses_serif_font(self):
        """position-tool.html renders Playfair Display — must map to 'serif'."""
        recipe = build_recipe()
        overlay = _only_overlay(_slot(recipe, 4))
        assert overlay["font_style"] == "serif"

    def test_y_position_below_peru(self):
        recipe = build_recipe()
        welcome = _only_overlay(_slot(recipe, 4))
        peru = _subject_overlay(_slot(recipe, 5))
        assert welcome["position_y_frac"] == pytest.approx(_seed.WELCOME_Y_FRAC)
        assert welcome["position_y_frac"] > peru["position_y_frac"], (
            "Welcome-to must sit below PERU (larger y_frac) — got the inverse, "
            "title would render with the subtitle on top"
        )

    def test_stays_visible_through_slot_end(self):
        """Without this, 'Welcome to' fades early and slot 4 runs with no text
        before slot 5's location title cuts in. Extending end_s to the slot
        duration closes that visual dead air."""
        recipe = build_recipe()
        slot4 = _slot(recipe, 4)
        welcome = _only_overlay(slot4)
        assert welcome["end_s"] == pytest.approx(slot4["target_duration_s"]), (
            f"'Welcome to' end_s={welcome['end_s']} leaves "
            f"{slot4['target_duration_s'] - welcome['end_s']:.2f}s of text-less "
            f"gap before slot 5's location title appears"
        )

    def test_starts_at_slot_start_to_match_reference_solo_window(self):
        """Reference holds 'Welcome to' alone for ~1.05s before BRAZIL appears.
        With slot 4 at 1.1s, welcome must start at 0.0s to give the full slot
        duration as the welcome-alone window. Starting later (e.g., 0.5s) only
        gives ~0.6s of welcome-alone, which is visibly shorter than reference."""
        recipe = build_recipe()
        slot4 = _slot(recipe, 4)
        welcome = _only_overlay(slot4)
        assert welcome["start_s"] == 0.0, (
            f"'Welcome to' start_s={welcome['start_s']} delays appearance into "
            f"the slot. Reference holds welcome alone for ~1.05s before BRAZIL "
            f"appears — start at 0.0 to use the full slot-4 duration as the "
            f"welcome-alone window."
        )

    def test_effect_is_none_for_clean_merge_with_slot_5(self):
        """`_collect_absolute_overlays` cross-slot-merges this welcome with
        slot 5's welcome (same text, same y, gap=0). The merged overlay
        inherits the LATER slot's effect. If slot 4 declares 'fade-in' but
        slot 5 declares 'none', the merged overlay silently loses the fade.
        Declare 'none' on both sides to keep the recipe honest about what
        actually renders."""
        recipe = build_recipe()
        slot4 = _slot(recipe, 4)
        welcome = _only_overlay(slot4)
        assert welcome["effect"] == "none", (
            f"slot 4 welcome effect={welcome['effect']!r} will be stripped at "
            f"cross-slot merge time (slot 5 welcome has effect='none'). Set "
            f"effect='none' here so the recipe matches the merged render."
        )


class TestSlot5DualOverlay:
    """Slot 5 carries a short-lived 'Welcome to' prefix alongside the location
    title for the first ~0.6s so the prefix stays on screen the moment the
    title appears. Without this co-render, the visual feels like a hard text
    swap on the slot 4 → 5 cut. With it, the prefix smoothly carries across
    the cut and then fades while the title font-cycles into the curtain."""

    def test_slot_5_has_exactly_two_overlays(self):
        recipe = build_recipe()
        overlays = _slot(recipe, 5).get("text_overlays") or []
        assert len(overlays) == 2, (
            f"Slot 5 must declare exactly 2 overlays (Welcome-to prefix + "
            f"location title), got {len(overlays)}"
        )

    def test_prefix_co_renders_through_brazil_phase(self):
        """Frame diff of brazil.mp4 (2026-05-10) shows 'Welcome to' visible
        inside/under the BRAZIL letters from BRAZIL onset (~5.3s) through
        ~8.5s — about 3.2s of co-render. Slot 5 must keep welcome alive
        for at least 3s so the merged welcome span (slot 4 + slot 5 start)
        covers the reference window."""
        recipe = build_recipe()
        prefix = _prefix_overlay(_slot(recipe, 5))
        assert prefix is not None, "slot 5 missing the Welcome-to prefix overlay"
        assert prefix["start_s"] == 0.0
        assert prefix["end_s"] >= 3.0, (
            f"Prefix end_s={prefix['end_s']} fades before BRAZIL finishes. "
            f"Reference holds welcome under BRAZIL for ~3.5s — extend end_s "
            f"to ≥3.0 so the cross-slot-merged welcome span matches."
        )
        assert prefix["end_s"] <= 5.5, (
            f"Prefix end_s={prefix['end_s']} can't exceed slot 5 duration "
            f"(5.5s); orchestrator silently clamps but the recipe would be "
            f"dishonest about what renders."
        )

    def test_prefix_styling_matches_slot_4(self):
        """The slot-5 prefix must look IDENTICAL to slot 4's 'Welcome to' so
        the cross-cut continuation reads as one continuous reveal."""
        recipe = build_recipe()
        slot4_prefix = _only_overlay(_slot(recipe, 4))
        slot5_prefix = _prefix_overlay(_slot(recipe, 5))
        assert slot5_prefix["text"] == slot4_prefix["text"] == "Welcome to"
        assert slot5_prefix["text_size_px"] == slot4_prefix["text_size_px"]
        assert slot5_prefix["text_color"].upper() == slot4_prefix["text_color"].upper()
        assert slot5_prefix["font_style"] == slot4_prefix["font_style"]
        assert slot5_prefix["position_y_frac"] == pytest.approx(slot4_prefix["position_y_frac"])

    def test_prefix_and_title_stack_correctly(self):
        recipe = build_recipe()
        prefix = _prefix_overlay(_slot(recipe, 5))
        title = _subject_overlay(_slot(recipe, 5))
        assert prefix["position_y_frac"] > title["position_y_frac"], (
            "Prefix must sit below the title (larger y_frac in the inverted "
            "coordinate system); otherwise the small white text covers the "
            "jumbo location title"
        )


class TestFontCycleAccel:
    """Per-frame font-cycle analysis of brazil.mp4 over the full 6s title
    window (analyze_brazil_animation.py, 2026-05-10) showed the reference
    has a slow-then-fast accel ramp:
      - 0.0s–2.8s into BRAZIL: ~0.132s interval (slow phase)
      - 2.8s–end:              ~0.066s interval (fast phase)

    An earlier commit set accel_at_s=0.0 based on a narrow zoom clip
    (yazıörnek.mp4) that only captured the fast half. The wider sample
    surfaced the slow ramp the zoom missed. Pin accel_at_s=2.8 so the
    cycle matches the reference's two-phase tempo."""

    def test_subject_has_explicit_accel_at_s(self):
        recipe = build_recipe()
        subject = _subject_overlay(_slot(recipe, 5))
        assert subject["font_cycle_accel_at_s"] is not None, (
            "Without an explicit value, the orchestrator's auto-injected accel "
            "fires at slot_end - animate_s, leaving the first ~4s of the slot "
            "in slow-cycle mode — reference accel kicks in at ~2.8s"
        )

    def test_accel_at_s_matches_reference_slow_fast_ramp(self):
        """Reference cycle runs slow (~0.132s) for first ~2.8s then fast
        (~0.066s) for the remainder. Pin accel_at_s=2.8 so the cycle phase
        boundary lines up with the reference's tempo shift. Drifting the
        value silently changes the slow/fast split and breaks the rhythmic
        feel users called out."""
        recipe = build_recipe()
        subject = _subject_overlay(_slot(recipe, 5))
        accel = subject["font_cycle_accel_at_s"]
        assert accel == pytest.approx(2.8), (
            f"font_cycle_accel_at_s={accel} doesn't match the reference's "
            f"2.8s slow-phase / fast-phase boundary. Set accel_at_s=2.8 so "
            f"the first 2.8s of BRAZIL cycles at FONT_CYCLE_INTERVAL_S "
            f"(0.15s) and the remainder at FONT_CYCLE_FAST_INTERVAL_S (0.07s)."
        )


class TestSlot6NoOverlay:
    """Reference video has no joint caption after the slot-5 curtain — the bars
    reopen straight onto b-roll. Slot 6 keeps its dissolve transition so the
    visual handoff is smooth, but it must render zero text overlays."""

    def test_no_text_overlays(self):
        recipe = build_recipe()
        slot6 = _slot(recipe, 6)
        assert slot6.get("text_overlays") == [], (
            "Slot 6 must not render a joint caption — reference shows direct "
            "cut from curtain-close to b-roll, no 'Welcome to {LOCATION}' text"
        )

    def test_transition_is_hard_cut_after_curtain(self):
        """The orchestrator force-overrides any post-interstitial transition
        to "none" (template_orchestrate.py:1677), regardless of what the
        recipe declares. Declaring "hard-cut" here keeps the recipe honest
        about what actually renders. Declaring "dissolve" here would lie —
        the recipe would say one thing while the rendered video does another.
        Reference video also shows a hard reopen from the curtain to b-roll
        with no fade."""
        recipe = build_recipe()
        slot6 = _slot(recipe, 6)
        assert slot6["transition_in"] == "hard-cut", (
            f"Slot-6 transition_in={slot6['transition_in']!r} disagrees with "
            f"what the orchestrator actually renders after a curtain-close "
            f"interstitial (always 'none'/hard-cut). Declare 'hard-cut' so "
            f"the recipe matches reality."
        )


class TestTitlePhaseDurations:
    """The title phase (slot 4 + slot 5) was rebalanced after frame-by-frame
    analysis of the reference video showed a major timing mismatch: the
    reference holds 'Welcome to' for ~0.7s and BRAZIL for ~5.8s, but the
    original recipe held Welcome-to for 3s and BRAZIL for only 2.73s. The
    BRAZIL phase was too short for font-cycling to read as rhythmic and the
    curtain consumed 60% of it. These tests pin the new balance:
    short Welcome-to (1.5s slot), long BRAZIL (5.5s slot), curtain ≤ 30% of
    the BRAZIL phase."""

    def test_welcome_to_slot_is_brief(self):
        recipe = build_recipe()
        slot4 = _slot(recipe, 4)
        assert slot4["target_duration_s"] <= 2.0, (
            f"Slot 4 (Welcome-to) duration={slot4['target_duration_s']}s — "
            f"reference holds Welcome-to for ~0.7s, slot 4 should be brief "
            f"(under 2s) so the title reveal doesn't feel dragged."
        )

    def test_brazil_slot_has_room_to_breathe(self):
        recipe = build_recipe()
        slot5 = _slot(recipe, 5)
        assert slot5["target_duration_s"] >= 4.5, (
            f"Slot 5 (BRAZIL/location) duration={slot5['target_duration_s']}s "
            f"— reference holds BRAZIL for ~5.8s. Anything under 4.5s means "
            f"the font-cycling has insufficient time to read as rhythmic and "
            f"the curtain ends up dominating the phase."
        )

    def test_curtain_is_minority_of_brazil_phase(self):
        """Reference curtain is ~28% of BRAZIL phase. Above 40% feels rushed."""
        recipe = build_recipe()
        slot5 = _slot(recipe, 5)
        inter = recipe["interstitials"][0]
        curtain_fraction = inter["animate_s"] / slot5["target_duration_s"]
        assert curtain_fraction <= 0.40, (
            f"Curtain is {curtain_fraction:.0%} of slot 5 — should be ≤40% so "
            f"font-cycling has uncovered screen time before bars close. "
            f"Reference is 28%."
        )

    def test_total_duration_close_to_music(self):
        """Music track is 21.4s. Total slot duration should be within 0.5s
        of that so neither audio nor video has an awkward dead zone."""
        recipe = build_recipe()
        total = sum(s["target_duration_s"] for s in recipe["slots"])
        # Music duration from the extracted track (templates/dimplespassport-edit-music.m4a)
        MUSIC_DURATION_S = 21.40
        assert abs(total - MUSIC_DURATION_S) <= 0.5, (
            f"Total slot duration {total:.2f}s diverges >0.5s from music "
            f"length {MUSIC_DURATION_S}s. Either trim a b-roll slot or "
            f"accept a brief silent/black tail."
        )

    def test_brazil_drops_on_music_beat_8(self):
        """The edit music's bass drop is at t=5.085s (beat 8, followed by
        a 3.4s silence where BRAZIL holds and cycles). Slot 5 must start
        within 100ms of that beat or the title misses the hook moment and
        falls into dead air between beats. This was the defect in earlier
        renders: BRAZIL appeared at 3.49s (between beats 4 and 5)."""
        BEAT_8_S = 5.085
        TOLERANCE_S = 0.1
        recipe = build_recipe()
        slots = sorted(recipe["slots"], key=lambda s: s["position"])
        slot_5_start = sum(s["target_duration_s"] for s in slots if s["position"] < 5)
        delta = slot_5_start - BEAT_8_S
        assert abs(delta) <= TOLERANCE_S, (
            f"Slot 5 (BRAZIL) starts at {slot_5_start:.3f}s; beat 8 (the "
            f"music's bass drop) is at {BEAT_8_S}s. Delta {delta:+.3f}s "
            f"exceeds {TOLERANCE_S}s tolerance — the title will miss the "
            f"hook moment. Adjust slots 1-3 to push slot 5 start onto the "
            f"beat."
        )


class TestSlot1NotBelowOrchestratorFloor:
    """template_orchestrate.py floors `slot_target_dur` at 0.5s in three
    places (lines 1420, 1429, 1442) to keep the encoder + AAC frame size
    happy. Any seed slot below 0.5s silently rounds up at render time,
    shifting every later slot's start by the rounding delta and producing
    a "lag-like" feel where cuts arrive ~0.43s later than the recipe says.

    Pin slot 1 ≥ 0.5s so the floor never has to kick in. Anyone re-introducing
    the pathological 0.1s value will trip this test and read the docstring
    instead of shipping a broken render."""

    def test_no_slot_below_orchestrator_floor(self):
        ORCHESTRATOR_FLOOR_S = 0.5
        recipe = build_recipe()
        for slot in recipe["slots"]:
            dur = slot["target_duration_s"]
            assert dur >= ORCHESTRATOR_FLOOR_S, (
                f"Slot {slot['position']} target_duration_s={dur} is below "
                f"the orchestrator's hard floor at {ORCHESTRATOR_FLOOR_S}s "
                f"(template_orchestrate.py:1420). Render will silently round "
                f"up and shift every later slot by the delta — visible as a "
                f"'lag' between recipe-declared and actual cut points."
            )


class TestFontCycleFitsWithinFrameCap:
    """The font-cycle renderer caps PNG output at MAX_FONT_CYCLE_FRAMES to
    prevent runaway generation. When the cap is hit mid-slot, cycling stops
    and a static gap-fill PNG plays for the remainder — visible as the
    animation 'freezing' before the curtain finishes.

    Pin the math so future seed tweaks (longer slots, earlier accel) can't
    silently push the frame count past the cap and re-introduce the freeze.
    """

    def test_slot_5_fast_cycle_phase_fits_under_cap(self):
        from app.pipeline.text_overlay import (
            FONT_CYCLE_FAST_INTERVAL_S,
            FONT_CYCLE_INTERVAL_S,
            MAX_FONT_CYCLE_FRAMES,
        )

        recipe = build_recipe()
        slot5 = _slot(recipe, 5)
        overlay = _subject_overlay(slot5)
        accel_at = overlay["font_cycle_accel_at_s"]
        slot_dur = slot5["target_duration_s"]

        # Frames generated in the normal (pre-accel) phase
        normal_frames = max(0, accel_at) / FONT_CYCLE_INTERVAL_S
        # Frames generated in the fast (post-accel) phase
        fast_phase_dur = slot_dur - accel_at
        fast_frames = fast_phase_dur / FONT_CYCLE_FAST_INTERVAL_S
        total_frames = normal_frames + fast_frames

        assert total_frames <= MAX_FONT_CYCLE_FRAMES, (
            f"Slot 5 needs {total_frames:.1f} PNG frames "
            f"(normal: {normal_frames:.1f}, fast: {fast_frames:.1f}) but cap "
            f"is {MAX_FONT_CYCLE_FRAMES}. Cycling will stop mid-curtain and "
            f"the text will freeze for "
            f"{(total_frames - MAX_FONT_CYCLE_FRAMES) * FONT_CYCLE_FAST_INTERVAL_S:.2f}s. "
            f"Either shrink slot 5 / push accel later, or raise the cap "
            f"in text_overlay.py."
        )


class TestCurtainCloseAfterSlot5:
    """The reference video shows top+bottom black bars closing in over the
    location title between ~8s-11s, then reopening to b-roll. The recipe must
    declare an interstitial of type curtain-close after slot 5."""

    def test_interstitials_list_has_one_entry(self):
        recipe = build_recipe()
        assert len(recipe["interstitials"]) == 1, (
            "Recipe should declare exactly one interstitial — the curtain-close "
            "after slot 5"
        )

    def test_interstitial_targets_slot_5(self):
        recipe = build_recipe()
        inter = recipe["interstitials"][0]
        assert inter["after_slot"] == 5, (
            "Curtain must close after slot 5 (the {location} font-cycle reveal); "
            "after_slot=4 would close over 'Welcome to' and after_slot=6 would "
            "close over the dissolve into b-roll"
        )

    def test_interstitial_type_is_curtain_close(self):
        recipe = build_recipe()
        inter = recipe["interstitials"][0]
        assert inter["type"] == "curtain-close", (
            "Reference shows top+bottom bars closing in — the only interstitial "
            "type that renders that animation"
        )

    def test_animate_s_within_orchestrator_clamp(self):
        """The orchestrator clamps animate_s to slot_duration * _CURTAIN_MAX_RATIO
        (0.6). Setting it explicitly at the clamp boundary documents intent."""
        recipe = build_recipe()
        slot5 = _slot(recipe, 5)
        inter = recipe["interstitials"][0]
        max_animate = slot5["target_duration_s"] * 0.6
        assert inter["animate_s"] <= max_animate + 1e-6, (
            f"animate_s={inter['animate_s']} exceeds 60% clamp on slot-5 duration "
            f"({slot5['target_duration_s']}s × 0.6 = {max_animate}s) — orchestrator "
            f"will silently clamp; set the value explicitly instead"
        )

    def test_hold_s_zero_for_direct_dissolve(self):
        """hold_s>0 inserts a black-hold clip after the curtain. Reference goes
        straight to b-roll, so hold_s=0 — the dissolve into slot 6 fires next."""
        recipe = build_recipe()
        inter = recipe["interstitials"][0]
        assert inter["hold_s"] == 0.0


class TestCurtainSurvivesConsolidation:
    """consolidate_slots in template_matcher.py runs whenever the user
    uploads fewer unique clips than the recipe declares slots (very common —
    Dimples requires only 5 clips minimum but has 17 slots). The post-merge
    curtain validator (template_matcher.py:386) drops any curtain where
    `slot_dur * _CURTAIN_MAX_RATIO < _MIN_CURTAIN_ANIMATE_S` — for slot 5
    (2.73s) that's 1.638s < 4.0s, which would silently kill the curtain in
    almost every production job.

    The recipe must declare `min_slots == shot_count` so consolidation
    skips entirely (matcher rotates clips across all 17 slots instead).
    """

    def test_min_slots_equals_shot_count(self):
        recipe = build_recipe()
        assert recipe["min_slots"] == recipe["shot_count"], (
            f"min_slots={recipe['min_slots']} must equal shot_count="
            f"{recipe['shot_count']} so consolidate_slots preserves all "
            f"slots and does not drop the curtain-close on slot 5"
        )

    def test_curtain_survives_when_user_uploads_few_clips(self):
        """End-to-end consolidation contract: simulate a 5-clip job (the
        Dimples minimum) and assert the curtain interstitial is not dropped.
        This would have caught the prior bug where slot 5's animate_s clamp
        (1.638s) fell below _MIN_CURTAIN_ANIMATE_S (4.0s), silently dropping
        the curtain during consolidation."""
        from app.pipeline.agents.gemini_analyzer import ClipMeta, TemplateRecipe
        from app.pipeline.template_matcher import consolidate_slots

        recipe_dict = build_recipe()
        # Strip routing-only keys (matches _build_recipe in orchestrator)
        ROUTING_ONLY = {"template_kind", "has_intro_slot"}
        kwargs = {k: v for k, v in recipe_dict.items() if k not in ROUTING_ONLY}
        recipe = TemplateRecipe(**kwargs)

        # Simulate the minimum (5) clips a user can upload — only fields
        # consolidate_slots reads (clip_id for uniqueness count) need real
        # values. Other fields are required-positional but unused here.
        mock_clips = [
            ClipMeta(
                clip_id=f"clip-{i}",
                transcript="",
                hook_text="",
                hook_score=0.0,
                best_moments=[],
            )
            for i in range(5)
        ]

        consolidated = consolidate_slots(recipe, mock_clips)

        curtains = [
            i for i in consolidated.interstitials if i.get("type") == "curtain-close"
        ]
        assert len(curtains) == 1, (
            f"Curtain-close was dropped during consolidation "
            f"({len(curtains)} curtains survive, expected 1). Check that "
            f"min_slots equals shot_count so consolidation is skipped."
        )
        assert curtains[0]["after_slot"] == 5
        # All 17 slots must be preserved (no merging)
        assert len(consolidated.slots) == len(recipe.slots) == 17


class TestEditMusicWired:
    """The template's slot durations (0.1, 0.99, 0.9, 3.5, 2.73, 0.96, …) were
    hand-tuned to land on beats in a specific edit track. The seed must declare
    that track's GCS path on `audio_gcs_path` so the orchestrator's
    `_mix_template_audio()` replaces source-clip audio with it. Without this
    wiring, every Dimples job ships with the user's random source audio and
    loses the beat-synced feel that's the entire point of the template."""

    def test_module_declares_edit_music_path(self):
        """The seed module must expose a non-empty EDIT_MUSIC_GCS_PATH so
        re-seeds (and the prod backfill flow) always carry the music path."""
        assert hasattr(_seed, "EDIT_MUSIC_GCS_PATH"), (
            "Seed module must export EDIT_MUSIC_GCS_PATH; the row upsert reads it"
        )
        assert _seed.EDIT_MUSIC_GCS_PATH, (
            "EDIT_MUSIC_GCS_PATH is empty — orchestrator will skip "
            "_mix_template_audio and source-clip audio will play through"
        )

    def test_edit_music_path_is_audio_extension(self):
        """Guard against pointing at a video file by accident — the mixer
        expects an audio-only container."""
        path = _seed.EDIT_MUSIC_GCS_PATH.lower()
        assert path.endswith((".m4a", ".mp3", ".aac", ".wav", ".ogg")), (
            f"EDIT_MUSIC_GCS_PATH={path!r} does not look like an audio file; "
            f"mixer expects audio-only container, not a video"
        )

    def test_edit_music_path_lives_under_templates_prefix(self):
        """All template assets live under the `templates/` GCS prefix. This
        keeps the bucket layout consistent and makes asset audits easy."""
        assert _seed.EDIT_MUSIC_GCS_PATH.startswith("templates/"), (
            f"Expected templates/ prefix, got {_seed.EDIT_MUSIC_GCS_PATH!r}"
        )


class TestSubjectSubstitutionContract:
    """Slot 5's text MUST be an ALL-CAPS placeholder so _resolve_overlay_text
    in template_orchestrate.py substitutes the user's `inputs.location` at
    render time. If this drifts to a non-ALL-CAPS string, substitution silently
    no-ops and every job ships with the literal seed string instead of the
    user's location.

    The substitution heuristic itself is exercised in
    tests/tasks/test_template_orchestrate.py — this test guards the seed-side
    contract that makes that substitution possible.
    """

    def test_slot_5_text_is_all_caps_placeholder(self):
        recipe = build_recipe()
        overlay = _subject_overlay(_slot(recipe, 5))
        text = overlay["text"]
        assert text.isupper(), (
            f"Slot 5 text {text!r} is not ALL-CAPS — _resolve_overlay_text "
            f"requires the placeholder to be all-caps to detect it as a "
            f"location slot. Output videos will display {text!r} literally "
            f"instead of the user's location."
        )

    def test_required_input_key_matches_resolver(self):
        """The resolver in template_orchestrate.py reads inputs['location'].
        The seed must declare a required_input with key='location' so the
        upload form collects it and the resolver finds it."""
        keys = [spec["key"] for spec in _seed.REQUIRED_INPUTS]
        assert "location" in keys, (
            "Required-input key 'location' is the contract _resolve_user_subject "
            "depends on; renaming it breaks substitution silently"
        )


class TestRequiredInputs:
    """The seed must declare a `location` input so the template page renders
    a country/city field. The key name MUST match what
    _resolve_user_subject() in template_orchestrate.py reads."""

    def test_declares_single_location_input(self):
        assert len(_seed.REQUIRED_INPUTS) == 1
        spec = _seed.REQUIRED_INPUTS[0]
        assert spec["key"] == "location", (
            "key must be 'location' — _resolve_user_subject reads inputs.location"
        )

    def test_location_is_required(self):
        spec = _seed.REQUIRED_INPUTS[0]
        assert spec["required"] is True, (
            "Empty location would render the seed default ('PERU') — confusing failure"
        )

    def test_max_length_fits_long_country_names(self):
        spec = _seed.REQUIRED_INPUTS[0]
        # "Democratic Republic of the Congo" = 32 chars; renderer auto-shrinks
        # for overflow, so 30 is sufficient and keeps the input compact.
        assert 20 <= spec["max_length"] <= 50

    def test_has_user_facing_label_and_placeholder(self):
        spec = _seed.REQUIRED_INPUTS[0]
        assert spec["label"], "label is shown above the input"
        assert spec["placeholder"], "placeholder gives the user a concrete example"
