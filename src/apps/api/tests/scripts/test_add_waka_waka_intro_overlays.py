"""Unit tests for the patch_recipe helper in scripts/add_waka_waka_intro_overlays.py.

Pure-function coverage only — DB interaction is exercised manually via
`--apply` against a dev DB, same pattern as test_backfill_waka_waka_location.py.
"""
from __future__ import annotations

import copy

import pytest

from scripts.add_waka_waka_intro_overlays import (
    INTRO_OVERLAYS,
    PositionMismatchError,
    patch_recipe,
)


def _base_recipe(*, slot_count: int = 4) -> dict:
    """Minimal recipe shaped like prod: slots have position, target_duration_s,
    and an empty text_overlays array. Returns slot_count slots numbered 1..N."""
    return {
        "slots": [
            {
                "position": i + 1,
                "target_duration_s": 1.3 if i == 0 else 1.2 if i == 1 else 2.4,
                "slot_type": "hook" if i < 2 else "broll",
                "text_overlays": [],
            }
            for i in range(slot_count)
        ]
    }


class TestPatchRecipe:
    def test_adds_three_overlays_when_slots_empty(self):
        recipe = _base_recipe()
        patched, changes = patch_recipe(recipe)
        assert len(changes) == 3
        assert all(c[0] == "add" for c in changes)
        # Slot 0: "This" slide-up
        ov0 = patched["slots"][0]["text_overlays"][0]
        assert ov0["sample_text"] == "This"
        assert ov0["effect"] == "slide-up"
        assert ov0["text_color"] == "#FFFFFF"
        assert ov0["end_s"] == pytest.approx(1.3)
        assert ov0["subject_substitute"] is False
        # Slot 1: "is" slide-up
        ov1 = patched["slots"][1]["text_overlays"][0]
        assert ov1["sample_text"] == "is"
        assert ov1["effect"] == "slide-up"
        assert ov1["end_s"] == pytest.approx(1.2)
        assert ov1["subject_substitute"] is False
        # Slot 2: "AFRICA" font-cycle
        ov2 = patched["slots"][2]["text_overlays"][0]
        assert ov2["sample_text"] == "AFRICA"
        assert ov2["effect"] == "font-cycle"
        assert ov2["text_color"] == "#F4D03F"
        assert ov2["end_s"] == pytest.approx(2.4)
        assert ov2["subject_substitute"] is False

    def test_idempotent_when_overlays_already_present(self):
        recipe = _base_recipe()
        # First pass.
        patched_once, _ = patch_recipe(recipe)
        # Second pass on the already-patched recipe.
        patched_twice, changes = patch_recipe(patched_once)
        assert changes == []
        # Same number of overlays — no double-write.
        for idx in (0, 1, 2):
            assert len(patched_twice["slots"][idx]["text_overlays"]) == 1

    def test_upgrades_in_place_when_subject_substitute_missing(self):
        """REGRESSION: templates patched by an earlier version of this script
        have the intro overlays but lack the `subject_substitute: False` flag,
        so the resolver heuristic rewrites "This"/"AFRICA" to the user's
        location. Re-running the script must add the flag in place without
        duplicating the overlay or disturbing other fields."""
        recipe = _base_recipe()
        # Simulate the legacy state: overlays present but missing the flag.
        recipe["slots"][0]["text_overlays"] = [
            {"sample_text": "This", "effect": "slide-up", "text_color": "#FFFFFF"}
        ]
        recipe["slots"][1]["text_overlays"] = [
            {"sample_text": "is", "effect": "slide-up", "text_color": "#FFFFFF"}
        ]
        recipe["slots"][2]["text_overlays"] = [
            {"sample_text": "AFRICA", "effect": "font-cycle", "text_color": "#F4D03F"}
        ]
        patched, changes = patch_recipe(recipe)
        # All three got upgraded, none added.
        assert len(changes) == 3
        assert all(c[0] == "upgrade" for c in changes)
        # Each overlay now has the flag — and the existing fields survived.
        for idx, sample in [(0, "This"), (1, "is"), (2, "AFRICA")]:
            ovs = patched["slots"][idx]["text_overlays"]
            assert len(ovs) == 1, f"slot {idx} must not be duplicated"
            ov = ovs[0]
            assert ov["sample_text"] == sample
            assert ov["subject_substitute"] is False
            # The fields the operator hand-set survived the upgrade.
            assert ov.get("text_color") in ("#FFFFFF", "#F4D03F")

    def test_upgrade_only_fires_when_flag_is_truthy_or_absent(self):
        """If the flag is already False, no change is recorded."""
        recipe = _base_recipe()
        recipe["slots"][0]["text_overlays"] = [
            {"sample_text": "This", "effect": "slide-up", "subject_substitute": False}
        ]
        recipe["slots"][1]["text_overlays"] = [
            {"sample_text": "is", "effect": "slide-up", "subject_substitute": False}
        ]
        recipe["slots"][2]["text_overlays"] = [
            {"sample_text": "AFRICA", "effect": "font-cycle", "subject_substitute": False}
        ]
        _, changes = patch_recipe(recipe)
        assert changes == []

    def test_upgrade_fires_when_flag_is_explicitly_true(self):
        """A misconfigured overlay with subject_substitute=True still gets
        downgraded to False — we never want intro overlays substituted."""
        recipe = _base_recipe()
        recipe["slots"][0]["text_overlays"] = [
            {"sample_text": "This", "subject_substitute": True}
        ]
        patched, changes = patch_recipe(recipe)
        upgrades = [c for c in changes if c[0] == "upgrade"]
        assert len(upgrades) == 1
        assert upgrades[0][3] == "This"
        assert patched["slots"][0]["text_overlays"][0]["subject_substitute"] is False

    def test_partial_backfill_adds_missing_and_upgrades_existing(self):
        """If slot 0 already has 'This' but slot 2 doesn't have AFRICA, the
        legacy 'This' gets upgraded in place (no duplicate) AND the missing
        intros get added."""
        recipe = _base_recipe()
        recipe["slots"][0]["text_overlays"] = [
            {"sample_text": "This", "effect": "slide-up"}
        ]
        patched, changes = patch_recipe(recipe)
        # 1 upgrade (slot 0 "This" gets subject_substitute=False) + 2 adds.
        assert len(changes) == 3
        kinds = sorted(c[0] for c in changes)
        assert kinds == ["add", "add", "upgrade"]
        samples = sorted(c[3] for c in changes)
        assert samples == ["AFRICA", "This", "is"]
        # Slot 0's existing overlay survived — same one, with the flag added.
        slot0 = patched["slots"][0]["text_overlays"]
        assert len(slot0) == 1
        assert slot0[0]["sample_text"] == "This"
        assert slot0[0]["subject_substitute"] is False
        assert slot0[0]["effect"] == "slide-up"  # operator field preserved

    def test_appends_when_slot_has_other_overlay(self):
        """If a slot already has an unrelated overlay (e.g. admin-added),
        append rather than skip — author's overlay survives, ours joins it."""
        recipe = _base_recipe()
        recipe["slots"][0]["text_overlays"] = [
            {"sample_text": "Welcome to", "effect": "fade-in"}
        ]
        patched, changes = patch_recipe(recipe)
        assert len(changes) == 3
        # Slot 0 now has both the welcome overlay AND "This".
        slot0_ovs = patched["slots"][0]["text_overlays"]
        assert len(slot0_ovs) == 2
        samples = sorted(ov["sample_text"] for ov in slot0_ovs)
        assert samples == ["This", "Welcome to"]

    def test_position_mismatch_raises(self):
        """Slot 0 with position=5 means slot order is corrupted — abort."""
        recipe = _base_recipe()
        recipe["slots"][0]["position"] = 5
        with pytest.raises(PositionMismatchError) as exc_info:
            patch_recipe(recipe)
        assert "position 5" in str(exc_info.value)
        assert "expected 1" in str(exc_info.value)

    def test_too_few_slots_raises(self):
        """Recipe with only 2 slots can't host the slot-3 AFRICA overlay."""
        recipe = _base_recipe(slot_count=2)
        with pytest.raises(PositionMismatchError) as exc_info:
            patch_recipe(recipe)
        assert "at least 3" in str(exc_info.value)

    def test_preserves_other_slot_overlays(self):
        """Overlays on slots 11/13/14/15 (the existing 'This time for Africa'
        / 'shukran Africa!' set) must not be touched."""
        recipe = _base_recipe(slot_count=16)
        # Put real-shape overlays on slots that aren't in our intro range.
        recipe["slots"][10]["text_overlays"] = [
            {"sample_text": "This time for Africa", "effect": "pop-in"}
        ]
        recipe["slots"][14]["text_overlays"] = [
            {"sample_text": "shukran Africa!", "effect": "bounce"},
        ]
        patched, changes = patch_recipe(recipe)
        assert len(changes) == 3
        # Untouched.
        assert patched["slots"][10]["text_overlays"] == [
            {"sample_text": "This time for Africa", "effect": "pop-in"}
        ]
        assert patched["slots"][14]["text_overlays"] == [
            {"sample_text": "shukran Africa!", "effect": "bounce"}
        ]

    def test_does_not_mutate_input(self):
        recipe = _base_recipe()
        before = copy.deepcopy(recipe)
        patch_recipe(recipe)
        assert recipe == before, "patch_recipe must not mutate its argument"

    def test_handles_missing_text_overlays_field(self):
        """Some old recipes lack the text_overlays key entirely."""
        recipe = _base_recipe()
        del recipe["slots"][0]["text_overlays"]
        patched, changes = patch_recipe(recipe)
        assert len(changes) == 3
        assert patched["slots"][0]["text_overlays"][0]["sample_text"] == "This"

    def test_handles_null_text_overlays_field(self):
        """Some old recipes have text_overlays: null."""
        recipe = _base_recipe()
        recipe["slots"][0]["text_overlays"] = None
        patched, changes = patch_recipe(recipe)
        assert len(changes) == 3
        assert patched["slots"][0]["text_overlays"][0]["sample_text"] == "This"


class TestOverlaySpecs:
    """Sanity checks on the constants so a typo doesn't reach production."""

    def test_three_overlays_defined(self):
        assert len(INTRO_OVERLAYS) == 3

    def test_slot_indices_are_0_1_2(self):
        assert [s["_slot_index"] for s in INTRO_OVERLAYS] == [0, 1, 2]

    def test_expected_positions_are_1_2_3(self):
        assert [s["_expected_position"] for s in INTRO_OVERLAYS] == [1, 2, 3]

    def test_africa_uses_font_cycle(self):
        africa = next(s for s in INTRO_OVERLAYS if s["sample_text"] == "AFRICA")
        assert africa["effect"] == "font-cycle"
        # Maize/gold — must match _LABEL_CONFIG["subject"]'s default.
        assert africa["text_color"] == "#F4D03F"

    def test_this_and_is_use_slide_up(self):
        for sample in ("This", "is"):
            spec = next(s for s in INTRO_OVERLAYS if s["sample_text"] == sample)
            assert spec["effect"] == "slide-up"
            assert spec["text_color"] == "#FFFFFF"

    def test_all_overlays_have_subject_substitute_false(self):
        """Without this, the resolver heuristic rewrites our intro text to
        the user's location input. Compile-time guard on the constant."""
        for spec in INTRO_OVERLAYS:
            assert spec.get("subject_substitute") is False, (
                f"overlay {spec['sample_text']!r} is missing "
                "subject_substitute=False — resolver heuristic will rewrite it"
            )
