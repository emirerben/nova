"""Recipe contract tests for the Rule of Thirds template seed.

These exist because the visual rhythm of the template lives in the recipe data,
not in code — and bad data passes silently. A regression here means every video
rendered from this template ships with the wrong grid timing.
"""
import importlib.util
import os
import sys

import pytest

# Load seed_rule_of_thirds.py from the scripts/ dir without a package install
_SEED_PATH = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "seed_rule_of_thirds.py"
))
_spec = importlib.util.spec_from_file_location("seed_rule_of_thirds", _SEED_PATH)
_seed = importlib.util.module_from_spec(_spec)
sys.modules["seed_rule_of_thirds"] = _seed
_spec.loader.exec_module(_seed)
build_recipe = _seed.build_recipe


def _slot_duration(slot: dict) -> float:
    return float(slot.get("target_duration_s", slot.get("target_duration", 0.0)))


class TestGridStartsWhiteOnSceneCut:
    """Every slot must show a white grid at t=0 — red highlight only kicks in
    later in the slot. Cuts that flash red on frame 1 collide with the visual
    scene change and ruin the beat-accent feel of the reference TikTok."""

    def test_every_b_roll_slot_starts_white(self):
        recipe = build_recipe()
        b_rolls = [s for s in recipe["slots"] if s.get("slot_type") == "broll"]
        assert b_rolls, "expected at least one b-roll slot in recipe"

        for slot in b_rolls:
            windows = slot.get("grid_highlight_windows")
            assert windows, (
                f"slot {slot.get('position')} (b-roll) has no grid_highlight_windows "
                f"— red highlight would default to sustained or off, both wrong"
            )
            first_start = min(w[0] for w in windows)
            assert first_start > 0.0, (
                f"slot {slot.get('position')} starts with red highlight at "
                f"t={first_start}s — must be > 0 so the cut shows white first"
            )

    def test_hook_slot_starts_white(self):
        """Slot 1 (hook) hosts the title overlay; the title fades in over a
        clean white grid, red blinks only land on later beats."""
        recipe = build_recipe()
        hooks = [s for s in recipe["slots"] if s.get("slot_type") == "hook"]
        assert hooks, "expected exactly one hook slot in recipe"

        for slot in hooks:
            windows = slot.get("grid_highlight_windows") or []
            if not windows:
                continue  # no highlight at all → trivially white throughout
            first_start = min(w[0] for w in windows)
            assert first_start > 0.0, (
                f"hook slot starts with red at t={first_start}s — must be > 0"
            )

    def test_first_white_phase_is_perceptible(self):
        """The white phase before the first red flash must be long enough to
        actually read as 'white' to the eye. < 100ms (3 frames at 30fps) feels
        like the cut itself flashed red — defeats the whole point."""
        recipe = build_recipe()
        b_rolls = [s for s in recipe["slots"] if s.get("slot_type") == "broll"]
        MIN_WHITE_PHASE_S = 0.1
        for slot in b_rolls:
            windows = slot.get("grid_highlight_windows") or []
            if not windows:
                continue
            first_start = min(w[0] for w in windows)
            assert first_start >= MIN_WHITE_PHASE_S, (
                f"slot {slot.get('position')} red flash starts at {first_start}s "
                f"— too close to cut to perceive a white phase first "
                f"(need >= {MIN_WHITE_PHASE_S}s)"
            )


class TestHighlightWindowsWithinSlotBounds:
    """A window past slot end gets clipped silently by FFmpeg's enable= clause
    (or worse, never fires). Catch it at recipe time."""

    @pytest.mark.parametrize("slot_type", ["hook", "broll"])
    def test_windows_fit_inside_slot_duration(self, slot_type):
        recipe = build_recipe()
        slots = [s for s in recipe["slots"] if s.get("slot_type") == slot_type]
        for slot in slots:
            dur = _slot_duration(slot)
            windows = slot.get("grid_highlight_windows") or []
            for start, end in windows:
                assert 0.0 <= start < end <= dur, (
                    f"slot {slot.get('position')} window [{start}, {end}] "
                    f"escapes slot duration {dur}s"
                )
