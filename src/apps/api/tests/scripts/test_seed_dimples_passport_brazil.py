"""Recipe contract tests for the Dimples Passport Brazil template seed.

The recipe shape and overlay tuning are intentionally pinned here. The
April 15 reference render relied on:

  - Slot 4 carrying BOTH the small serif "Welcome to" subtitle AND the jumbo
    "PERU" font-cycle title on the same long shot.
  - Slots 5-8 repeating a cinematic letterbox montage (has_narrowing=true)
    with PERU font-cycle labels on each beat.

The May 9 simplification dropped the multi-slot PERU + letterbox and broke
the signature look. These tests guard against silently reverting back.

The PERU/Welcome-to position + size + color come from the position-tool in
src/apps/web/public/position-tool.html; the constants in the seed
(PERU_SIZE_PX, PERU_Y_FRAC, PERU_COLOR, WELCOME_*) must round-trip into
every PERU/Welcome overlay so the admin editor preview matches the render.
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


def _peru_overlay(slot: dict) -> dict:
    """Return the PERU overlay on a slot (slots 5-8 have one; slot 4 has two)."""
    for ov in slot.get("text_overlays") or []:
        if ov.get("text") == "PERU":
            return ov
    raise AssertionError(f"slot {slot['position']} has no PERU overlay")


def _welcome_overlay(slot: dict) -> dict:
    for ov in slot.get("text_overlays") or []:
        if ov.get("text") == "Welcome to":
            return ov
    raise AssertionError(f"slot {slot['position']} has no 'Welcome to' overlay")


class TestRecipeShape:
    """The recipe must have 18 slots matching the April 15 reference shape."""

    def test_slot_count_is_18(self):
        recipe = build_recipe()
        # 18 slots is the April 15 / Gemini-analyzed reference shape. The
        # May 9 simplification collapsed this to 17 and dropped a body slot.
        assert len(recipe["slots"]) == 18
        assert recipe["shot_count"] == 18

    def test_positions_are_dense_and_one_indexed(self):
        recipe = build_recipe()
        positions = [s["position"] for s in recipe["slots"]]
        assert positions == list(range(1, 19))

    def test_total_duration_around_21s(self):
        recipe = build_recipe()
        # April 15 ref is 21.0s exactly; we allow ±0.5s for tuning headroom.
        assert 20.5 <= recipe["total_duration_s"] <= 21.5


class TestSlot4CombinedTitle:
    """Slot 4 is the long combined-title shot: 'Welcome to' fades in,
    then 'PERU' font-cycles on top. Both share the same 5.2s slot."""

    def test_slot_duration_is_long_combined_shot(self):
        recipe = build_recipe()
        assert _slot(recipe, 4)["target_duration_s"] == pytest.approx(5.2)

    def test_has_both_overlays(self):
        recipe = build_recipe()
        overlays = _slot(recipe, 4)["text_overlays"]
        texts = {ov["text"] for ov in overlays}
        assert texts == {"Welcome to", "PERU"}, (
            "slot 4 must host both 'Welcome to' and 'PERU' — that's the "
            "April 15 reference; May 9 split them onto separate slots"
        )

    def test_welcome_to_is_small_serif_white(self):
        recipe = build_recipe()
        welcome = _welcome_overlay(_slot(recipe, 4))
        assert welcome["text_size_px"] == _seed.WELCOME_SIZE_PX == 48
        assert welcome["text_color"].upper() == _seed.WELCOME_COLOR.upper() == "#FFFFFF"
        assert welcome["font_style"] == "serif"
        assert welcome["effect"] == "fade-in"
        assert welcome["position_y_frac"] == pytest.approx(_seed.WELCOME_Y_FRAC)

    def test_peru_is_jumbo_yellow_font_cycle(self):
        recipe = build_recipe()
        peru = _peru_overlay(_slot(recipe, 4))
        assert peru["text_size_px"] == _seed.PERU_SIZE_PX == 265, (
            "PERU dropped from jumbo (265px) — title screen no longer reads as a hook"
        )
        assert peru["text_color"].upper() == _seed.PERU_COLOR.upper() == "#F4D03F"
        assert peru["effect"] == "font-cycle"
        assert peru["position_y_frac"] == pytest.approx(_seed.PERU_Y_FRAC) == pytest.approx(0.45)

    def test_peru_starts_after_welcome_fades_in(self):
        """'Welcome to' should be visible briefly before 'PERU' enters."""
        recipe = build_recipe()
        welcome = _welcome_overlay(_slot(recipe, 4))
        peru = _peru_overlay(_slot(recipe, 4))
        assert peru["start_s"] > welcome["start_s"], (
            "PERU must enter after 'Welcome to' — otherwise the subtitle never reads"
        )

    def test_slot_4_has_no_narrowing(self):
        """The combined-title shot is full-frame; narrowing kicks in on slots 5-8."""
        recipe = build_recipe()
        for ov in _slot(recipe, 4)["text_overlays"]:
            assert ov["has_narrowing"] is False, (
                f"slot 4 {ov['text']!r} should not have narrowing — "
                "letterbox is for the slots 5-8 body montage"
            )


class TestSlots5To8LetterboxMontage:
    """Slots 5-8 are the cinematic letterbox montage — every shot beats with
    a PERU label cycling on a narrowed frame. Losing has_narrowing=true on
    any of these slots silently kills the April 15 signature look."""

    @pytest.mark.parametrize("position", [5, 6, 7, 8])
    def test_has_peru_label_with_narrowing(self, position):
        recipe = build_recipe()
        slot = _slot(recipe, position)
        overlays = slot["text_overlays"]
        assert len(overlays) == 1, (
            f"slot {position} must have exactly one PERU label overlay"
        )
        ov = overlays[0]
        assert ov["text"] == "PERU"
        assert ov["role"] == "label", (
            f"slot {position} PERU should be role=label (not hook) — it's a "
            "beat-synced body label, not the main title"
        )
        assert ov["effect"] == "font-cycle"
        assert ov["has_narrowing"] is True, (
            f"slot {position} lost has_narrowing=true — letterbox bars "
            "are the April 15 reference's signature title montage look"
        )

    @pytest.mark.parametrize("position", [5, 6, 7, 8])
    def test_peru_label_inherits_position_tool_styling(self, position):
        """Even on the body montage labels, the size/color/y_frac match the
        position-tool tuning so the title reads as one continuous identity."""
        recipe = build_recipe()
        ov = _peru_overlay(_slot(recipe, position))
        assert ov["text_size_px"] == _seed.PERU_SIZE_PX
        assert ov["text_color"].upper() == _seed.PERU_COLOR.upper()
        assert ov["position_y_frac"] == pytest.approx(_seed.PERU_Y_FRAC)
        assert ov["font_style"] == "sans"

    def test_letterbox_slots_are_short(self):
        """The montage cuts on beat — slots 5-8 stay under 1.5s each."""
        recipe = build_recipe()
        for position in [5, 6, 7, 8]:
            dur = _slot(recipe, position)["target_duration_s"]
            assert 0.3 <= dur <= 1.5, (
                f"slot {position} duration {dur}s is outside beat range; "
                "title labels won't sync to fast cuts"
            )


class TestBodyAndOutro:
    """Slots 9-17 are clean b-roll; slot 18 is the outro tail. None should
    carry text overlays — only slots 4-8 do."""

    @pytest.mark.parametrize("position", list(range(9, 19)))
    def test_no_text_overlays(self, position):
        recipe = build_recipe()
        slot = _slot(recipe, position)
        assert slot.get("text_overlays") == [], (
            f"slot {position} should have no overlays — only slots 4-8 carry text"
        )

    def test_slot_18_is_outro(self):
        recipe = build_recipe()
        assert _slot(recipe, 18)["slot_type"] == "outro"


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
