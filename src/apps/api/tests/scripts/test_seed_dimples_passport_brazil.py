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


class TestPeruHookSizing:
    """Slot 5 is the PERU font-cycle moment — must be the jumbo yellow heading."""

    def test_size_matches_position_tool_default(self):
        recipe = build_recipe()
        overlay = _only_overlay(_slot(recipe, 5))
        assert overlay["text"] == "PERU"
        assert overlay["text_size_px"] == _seed.PERU_SIZE_PX == 265, (
            "PERU dropped from jumbo (265px) — title screen no longer reads as a hook"
        )

    def test_color_matches_montserrat_yellow(self):
        recipe = build_recipe()
        overlay = _only_overlay(_slot(recipe, 5))
        assert overlay["text_color"].upper() == _seed.PERU_COLOR.upper() == "#F4D03F"

    def test_y_position_matches_tool(self):
        recipe = build_recipe()
        overlay = _only_overlay(_slot(recipe, 5))
        assert overlay["position_y_frac"] == pytest.approx(_seed.PERU_Y_FRAC) == pytest.approx(0.45)

    def test_uses_display_font_for_bold_serif(self):
        """Reference renders the location title in a BOLD yellow serif
        (PlayfairDisplay-Bold.ttf). The font registry maps:
          font_style="display" → PlayfairDisplay-Bold.ttf  (weight 700) ← USE THIS
          font_style="serif"   → PlayfairDisplay-Regular.ttf (weight 400) ← TOO THIN
        At 265px jumbo size the difference is dramatic — Regular looks
        anemic and breaks the template's signature look."""
        recipe = build_recipe()
        overlay = _only_overlay(_slot(recipe, 5))
        assert overlay["font_style"] == "display", (
            f"font_style={overlay['font_style']!r} resolves to a non-Bold "
            f"weight. Use 'display' to get PlayfairDisplay-Bold.ttf — the "
            f"Bold weight the reference video uses at 265px jumbo size."
        )

    def test_effect_is_font_cycle(self):
        recipe = build_recipe()
        overlay = _only_overlay(_slot(recipe, 5))
        assert overlay["effect"] == "font-cycle"


class TestWelcomeToHookSizing:
    """Slot 4 is the small serif "Welcome to" sitting just below PERU."""

    def test_size_matches_position_tool_default(self):
        recipe = build_recipe()
        overlay = _only_overlay(_slot(recipe, 4))
        assert overlay["text"] == "Welcome to"
        assert overlay["text_size_px"] == _seed.WELCOME_SIZE_PX == 48

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
        peru = _only_overlay(_slot(recipe, 5))
        assert welcome["position_y_frac"] == pytest.approx(_seed.WELCOME_Y_FRAC)
        assert welcome["position_y_frac"] > peru["position_y_frac"], (
            "Welcome-to must sit below PERU (larger y_frac) — got the inverse, "
            "title would render with the subtitle on top"
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
        overlay = _only_overlay(_slot(recipe, 5))
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
