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

    def test_uses_sans_font(self):
        """position-tool.html renders Montserrat 800 — must map to 'sans'
        in the font registry, not the serif fallback."""
        recipe = build_recipe()
        overlay = _only_overlay(_slot(recipe, 5))
        assert overlay["font_style"] == "sans"

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


class TestJoinedCaptionSlot6:
    """Slot 6 carries the joined caption shown after the dissolve into b-roll.
    Styling must match slot 5 so the editor preview reads as one continuous
    title rather than a sudden font change at the cut."""

    def test_uses_peru_styling(self):
        recipe = build_recipe()
        overlay = _only_overlay(_slot(recipe, 6))
        assert overlay["text"] == "Welcome to PERU"
        assert overlay["text_size_px"] == _seed.PERU_SIZE_PX
        assert overlay["text_color"].upper() == _seed.PERU_COLOR.upper()
        assert overlay["position_y_frac"] == pytest.approx(_seed.PERU_Y_FRAC)
