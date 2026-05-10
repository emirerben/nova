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

    def test_uses_serif_font_per_reference(self):
        """Reference video renders the location title in a yellow serif
        (Playfair Display Bold style) — not Montserrat sans. font_style='serif'
        maps to the bundled Playfair Display in assets/fonts/."""
        recipe = build_recipe()
        overlay = _only_overlay(_slot(recipe, 5))
        assert overlay["font_style"] == "serif", (
            "Slot-5 location title must render in serif (Playfair Display) "
            "to match the reference video — sans (Montserrat) is the wrong "
            "weight/glyph family for this template's signature look"
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

    def test_dissolve_transition_preserved(self):
        recipe = build_recipe()
        slot6 = _slot(recipe, 6)
        assert slot6["transition_in"] == "dissolve", (
            "Removing the overlay must not regress the dissolve — it carries "
            "the visual handoff from the curtain-closed title into b-roll"
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
