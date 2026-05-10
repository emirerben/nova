"""Unit tests for the patch_recipe helper in scripts/backfill_that_one_trip_to.py.

Covers the pure functions only — DB interaction is exercised manually via
`--apply` against a dev DB and noted in the plan's verification checklist.
"""
from __future__ import annotations

from scripts.backfill_that_one_trip_to import (
    FRAGMENT_MAP,
    REQUIRED_INPUTS,
    _overlay_sample,
    patch_recipe,
)


class TestOverlaySample:
    def test_returns_sample_text_when_set(self):
        assert _overlay_sample({"sample_text": "lon", "text": "ignored"}) == "lon"

    def test_falls_back_to_text_when_sample_empty(self):
        assert _overlay_sample({"sample_text": "", "text": "lon"}) == "lon"

    def test_strips_whitespace(self):
        assert _overlay_sample({"sample_text": "  lon  "}) == "lon"

    def test_empty_when_both_missing(self):
        assert _overlay_sample({}) == ""


class TestPatchRecipe:
    def test_tags_lon_don_london_fragments(self):
        recipe = {
            "slots": [
                {"text_overlays": [
                    {"sample_text": "lon"},
                    {"sample_text": "don"},
                ]},
                {"text_overlays": [
                    {"sample_text": "london"},
                ]},
            ],
        }
        patched, changes = patch_recipe(recipe)
        assert len(changes) == 3
        assert patched["slots"][0]["text_overlays"][0]["subject_part"] == "first_half"
        assert patched["slots"][0]["text_overlays"][1]["subject_part"] == "second_half"
        assert patched["slots"][1]["text_overlays"][0]["subject_part"] == "full"

    def test_case_insensitive_match(self):
        recipe = {"slots": [{"text_overlays": [
            {"sample_text": "LON"},
            {"sample_text": "Don"},
            {"sample_text": "LONDON"},
        ]}]}
        _, changes = patch_recipe(recipe)
        assert len(changes) == 3
        # Mapping is keyed by lowercase, but the original casing is preserved
        # in sample_text — only the new subject_part field is added.

    def test_falls_back_to_text_field(self):
        recipe = {"slots": [{"text_overlays": [
            {"sample_text": "", "text": "lon"},
        ]}]}
        patched, changes = patch_recipe(recipe)
        assert len(changes) == 1
        assert patched["slots"][0]["text_overlays"][0]["subject_part"] == "first_half"

    def test_unrelated_overlays_untouched(self):
        recipe = {"slots": [{"text_overlays": [
            {"sample_text": "Welcome to"},
            {"sample_text": "PERU"},
            {"sample_text": "discovering a hidden river"},
        ]}]}
        patched, changes = patch_recipe(recipe)
        assert changes == []
        for overlay in patched["slots"][0]["text_overlays"]:
            assert "subject_part" not in overlay

    def test_idempotent_on_already_tagged_recipe(self):
        recipe = {
            "slots": [{"text_overlays": [
                {"sample_text": "lon", "subject_part": "first_half"},
                {"sample_text": "don", "subject_part": "second_half"},
            ]}],
        }
        _, changes = patch_recipe(recipe)
        assert changes == []

    def test_re_tags_overlay_with_wrong_subject_part(self):
        """Operator hand-edited the wrong value? Backfill corrects it."""
        recipe = {
            "slots": [{"text_overlays": [
                {"sample_text": "lon", "subject_part": "second_half"},
            ]}],
        }
        patched, changes = patch_recipe(recipe)
        assert len(changes) == 1
        assert changes[0][3] == "second_half"  # old value
        assert changes[0][4] == "first_half"   # new value
        assert patched["slots"][0]["text_overlays"][0]["subject_part"] == "first_half"

    def test_does_not_mutate_input_recipe(self):
        recipe = {"slots": [{"text_overlays": [{"sample_text": "lon"}]}]}
        original = {"slots": [{"text_overlays": [{"sample_text": "lon"}]}]}
        patch_recipe(recipe)
        assert recipe == original

    def test_empty_recipe(self):
        patched, changes = patch_recipe({})
        assert patched == {}
        assert changes == []

    def test_recipe_with_empty_slots(self):
        patched, changes = patch_recipe({"slots": []})
        assert patched == {"slots": []}
        assert changes == []


def test_required_inputs_shape():
    """Sanity-check the constant matches the JSONB schema the API expects."""
    assert isinstance(REQUIRED_INPUTS, list)
    assert len(REQUIRED_INPUTS) == 1
    spec = REQUIRED_INPUTS[0]
    assert spec["key"] == "location"
    assert isinstance(spec["max_length"], int)
    assert isinstance(spec["required"], bool)


def test_fragment_map_keys_are_lowercase():
    """patch_recipe lowercases the sample before lookup, so the map MUST be lowercase."""
    for key in FRAGMENT_MAP:
        assert key == key.lower(), f"FRAGMENT_MAP key {key!r} is not lowercase"
