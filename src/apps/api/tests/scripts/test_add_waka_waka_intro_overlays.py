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

# Mirror the AFRICA cycle_fonts roster from the script. Updating both this
# constant and `cycle_fonts` in scripts/add_waka_waka_intro_overlays.py
# together is the contract — drift between the two is what the
# test_africa_uses_permanent_marker_gold_brush_cycle test guards against.
EXPECTED_AFRICA_CYCLE_FONTS = [
    "Permanent Marker",
    "Caveat Brush",
    "Shadows Into Light",
    "Kalam",
    "Indie Flower",
    "Architects Daughter",
]


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
        # Slot 2: "AFRICA" — six-font hand-drawn cycle at 250px in #FFD700
        # gold. 250px = ~86% of 1080 width with margin; 280px overflowed in
        # libass-rendered output. Gold reads cleaner than the empirical-
        # source amber (#EFC611) over dark backdrops.
        ov2 = patched["slots"][2]["text_overlays"][0]
        assert ov2["sample_text"] == "AFRICA"
        assert ov2["effect"] == "font-cycle"
        assert ov2["cycle_fonts"] == EXPECTED_AFRICA_CYCLE_FONTS
        assert ov2["font_family"] == "Permanent Marker"
        assert ov2["text_color"] == "#FFD700"
        assert ov2["text_size_px"] == 250
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

    def test_upgrades_in_place_when_enforced_fields_drift(self):
        """REGRESSION: templates patched by earlier versions of this script
        accumulate field-drift bugs over time:
          - missing `subject_substitute: False` -> resolver rewrites
            "This"/"AFRICA" to the user's location
          - on AFRICA: wrong effect (slide-up or full-registry font-cycle),
            wrong font (Montserrat), wrong color (#F4D03F amber default),
            missing cycle_fonts list
        Re-running must sync every _ENFORCED_UPGRADE_FIELDS value in place
        without duplicating overlays."""
        recipe = _base_recipe()
        # Simulate the legacy state: every bug we've found since v1.
        recipe["slots"][0]["text_overlays"] = [
            {"sample_text": "This", "effect": "slide-up", "text_color": "#FFFFFF"}
        ]
        recipe["slots"][1]["text_overlays"] = [
            {"sample_text": "is", "effect": "slide-up", "text_color": "#FFFFFF"}
        ]
        recipe["slots"][2]["text_overlays"] = [
            {
                "sample_text": "AFRICA",
                "effect": "slide-up",
                "text_color": "#F4D03F",
                "font_family": "Montserrat",
                "text_size_px": 250,
            }
        ]
        patched, changes = patch_recipe(recipe)
        # All three got upgraded, none added.
        assert len(changes) == 3
        assert all(c[0] == "upgrade" for c in changes)
        # Each overlay's subject_substitute is now False.
        for idx, sample in [(0, "This"), (1, "is"), (2, "AFRICA")]:
            ovs = patched["slots"][idx]["text_overlays"]
            assert len(ovs) == 1, f"slot {idx} must not be duplicated"
            ov = ovs[0]
            assert ov["sample_text"] == sample
            assert ov["subject_substitute"] is False
        # AFRICA-specific: font/color/size/effect/cycle_fonts all sync to spec.
        africa = patched["slots"][2]["text_overlays"][0]
        assert africa["effect"] == "font-cycle"
        assert africa["cycle_fonts"] == EXPECTED_AFRICA_CYCLE_FONTS
        assert africa["font_family"] == "Permanent Marker"
        assert africa["text_color"] == "#FFD700"
        assert africa["text_size_px"] == 250

    def test_upgrades_legacy_africa_styling_fields(self):
        """Tighter regression for AFRICA-specific drift across each enforced
        styling field. Each individual field, isolated, must still trigger
        an upgrade so partial-drift templates get fully synced on next
        backfill."""
        for drift_field, drift_value, expected in [
            ("effect", "slide-up", "font-cycle"),
            ("font_family", "Montserrat", "Permanent Marker"),
            ("text_color", "#F4D03F", "#FFD700"),
            ("text_size_px", 200, 250),
            ("cycle_fonts", None, EXPECTED_AFRICA_CYCLE_FONTS),
            ("cycle_fonts", ["Montserrat", "Outfit"], EXPECTED_AFRICA_CYCLE_FONTS),
        ]:
            recipe = _base_recipe()
            base_overlay = {
                "sample_text": "AFRICA",
                "effect": "font-cycle",
                "font_family": "Permanent Marker",
                "text_color": "#FFD700",
                "text_size_px": 250,
                "cycle_fonts": list(EXPECTED_AFRICA_CYCLE_FONTS),
                "subject_substitute": False,
            }
            if drift_value is None:
                base_overlay.pop(drift_field, None)
            else:
                base_overlay[drift_field] = drift_value
            recipe["slots"][2]["text_overlays"] = [base_overlay]
            patched, changes = patch_recipe(recipe)
            africa_upgrades = [c for c in changes if c[0] == "upgrade" and c[3] == "AFRICA"]
            assert len(africa_upgrades) == 1, (
                f"drift on {drift_field}={drift_value!r} must trigger upgrade"
            )
            assert patched["slots"][2]["text_overlays"][0][drift_field] == expected, (
                f"{drift_field} should sync to {expected!r}"
            )

    def test_upgrade_only_fires_when_enforced_fields_drift(self):
        """If every enforced field already matches the spec, no change is
        recorded. Tests the full enforced set: subject_substitute, effect,
        font_family, text_color, text_size_px, cycle_fonts."""
        recipe = _base_recipe()
        recipe["slots"][0]["text_overlays"] = [
            {
                "sample_text": "This",
                "effect": "slide-up",
                "subject_substitute": False,
                "font_family": "Montserrat",
                "text_color": "#FFFFFF",
                "text_size_px": 140,
            }
        ]
        recipe["slots"][1]["text_overlays"] = [
            {
                "sample_text": "is",
                "effect": "slide-up",
                "subject_substitute": False,
                "font_family": "Montserrat",
                "text_color": "#FFFFFF",
                "text_size_px": 140,
            }
        ]
        recipe["slots"][2]["text_overlays"] = [
            {
                "sample_text": "AFRICA",
                "effect": "font-cycle",
                "cycle_fonts": list(EXPECTED_AFRICA_CYCLE_FONTS),
                "subject_substitute": False,
                "font_family": "Permanent Marker",
                "text_color": "#FFD700",
                "text_size_px": 250,
            }
        ]
        _, changes = patch_recipe(recipe)
        assert changes == []

    def test_upgrade_fires_when_flag_is_explicitly_true(self):
        """A misconfigured overlay with subject_substitute=True still gets
        downgraded to False — we never want intro overlays substituted."""
        recipe = _base_recipe()
        recipe["slots"][0]["text_overlays"] = [{"sample_text": "This", "subject_substitute": True}]
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
        recipe["slots"][0]["text_overlays"] = [{"sample_text": "This", "effect": "slide-up"}]
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
        recipe["slots"][0]["text_overlays"] = [{"sample_text": "Welcome to", "effect": "fade-in"}]
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

    def test_africa_uses_permanent_marker_gold_brush_cycle(self):
        """AFRICA styling locked to match the morocco source video:
        - effect=font-cycle with a six-font hand-drawn cycle
          (Permanent Marker + Caveat Brush + Shadows Into Light + Kalam
          + Indie Flower + Architects Daughter). Six distinct hand faces
          give real frame-to-frame ink-flicker variation. cycle_fonts
          must stay in the hand-drawn lane — adding sans/serif/script
          fonts produces the jarring flicker the source never had.
        - font_family="Permanent Marker" (settle font; closest registry
          match to the source's hand-drawn lettering)
        - text_color="#FFD700" (gold; reads cleaner over dark backdrops
          than the empirical-source amber #EFC611)
        - text_size_px=250 (~86% of 1080 wide; 280px overflowed in
          libass-rendered output)"""
        africa = next(s for s in INTRO_OVERLAYS if s["sample_text"] == "AFRICA")
        assert africa["effect"] == "font-cycle"
        assert africa["cycle_fonts"] == EXPECTED_AFRICA_CYCLE_FONTS
        assert africa["font_family"] == "Permanent Marker"
        assert africa["text_color"] == "#FFD700"
        assert africa["text_size_px"] == 250

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
