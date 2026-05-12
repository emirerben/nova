"""Unit tests for the patch_recipe helper in scripts/backfill_waka_waka_location.py.

Covers the pure functions only — DB interaction is exercised manually via
`--apply` against a dev DB and noted in the script docstring.
"""
from __future__ import annotations

from scripts.backfill_waka_waka_location import (
    NEW_SAMPLE_TEXT,
    OUTRO_SAMPLES,
    OUTRO_TEMPLATE,
    REQUIRED_INPUTS,
    _overlay_sample,
    patch_recipe,
)


class TestOverlaySample:
    def test_returns_sample_text_when_set(self):
        assert _overlay_sample({"sample_text": "shukran Morocco!", "text": "ignored"}) \
            == "shukran Morocco!"

    def test_falls_back_to_text_when_sample_empty(self):
        assert _overlay_sample({"sample_text": "", "text": "shukran Morocco!"}) \
            == "shukran Morocco!"

    def test_strips_whitespace(self):
        assert _overlay_sample({"sample_text": "  shukran Morocco!  "}) \
            == "shukran Morocco!"

    def test_empty_when_both_missing(self):
        assert _overlay_sample({}) == ""


class TestPatchRecipe:
    def test_tags_morocco_outro(self):
        """Pre-backfill state: sample_text='shukran Morocco!', no template tag."""
        recipe = {"slots": [
            {"text_overlays": [{"role": "label", "sample_text": "Welcome to"}]},
            {"text_overlays": [{
                "role": "reaction",
                "text": "",
                "sample_text": "shukran Morocco!",
                "effect": "bounce",
            }]},
        ]}
        patched, changes = patch_recipe(recipe)
        assert len(changes) == 1
        outro = patched["slots"][1]["text_overlays"][0]
        assert outro["sample_text"] == NEW_SAMPLE_TEXT
        assert outro["subject_template"] == OUTRO_TEMPLATE
        # Other fields preserved.
        assert outro["role"] == "reaction"
        assert outro["effect"] == "bounce"

    def test_case_insensitive_match(self):
        recipe = {"slots": [{"text_overlays": [
            {"sample_text": "SHUKRAN MOROCCO!"},
            {"sample_text": "Shukran Morocco!"},
        ]}]}
        _, changes = patch_recipe(recipe)
        assert len(changes) == 2

    def test_falls_back_to_text_field(self):
        """Some prod recipes use `text` instead of `sample_text` for the literal."""
        recipe = {"slots": [{"text_overlays": [
            {"sample_text": "", "text": "shukran Morocco!"},
        ]}]}
        patched, changes = patch_recipe(recipe)
        assert len(changes) == 1
        ov = patched["slots"][0]["text_overlays"][0]
        assert ov["sample_text"] == NEW_SAMPLE_TEXT
        assert ov["subject_template"] == OUTRO_TEMPLATE

    def test_unrelated_overlays_untouched(self):
        recipe = {"slots": [{"text_overlays": [
            {"sample_text": "Welcome to"},
            {"sample_text": "This time for Africa"},
            {"sample_text": "We're all Africa"},
            {"sample_text": "AFRICA"},
        ]}]}
        patched, changes = patch_recipe(recipe)
        assert changes == []
        for overlay in patched["slots"][0]["text_overlays"]:
            assert "subject_template" not in overlay

    def test_idempotent_on_fully_backfilled_recipe(self):
        recipe = {"slots": [{"text_overlays": [{
            "sample_text": NEW_SAMPLE_TEXT,
            "subject_template": OUTRO_TEMPLATE,
        }]}]}
        _, changes = patch_recipe(recipe)
        assert changes == []

    def test_partial_backfill_re_tags(self):
        """Operator renamed sample_text but forgot subject_template — fix it."""
        recipe = {"slots": [{"text_overlays": [
            {"sample_text": "shukran Africa!"},  # renamed but no template
        ]}]}
        patched, changes = patch_recipe(recipe)
        assert len(changes) == 1
        ov = patched["slots"][0]["text_overlays"][0]
        assert ov["sample_text"] == NEW_SAMPLE_TEXT
        assert ov["subject_template"] == OUTRO_TEMPLATE

    def test_does_not_mutate_input_recipe(self):
        recipe = {"slots": [{"text_overlays": [{"sample_text": "shukran Morocco!"}]}]}
        original = {"slots": [{"text_overlays": [{"sample_text": "shukran Morocco!"}]}]}
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

    def test_recipe_without_outro_overlay(self):
        """Recipe hand-edited per PR #111 — no shukran overlay anywhere."""
        recipe = {"slots": [
            {"text_overlays": [{"sample_text": "This"}]},
            {"text_overlays": [{"sample_text": "is"}]},
            {"text_overlays": [{"sample_text": "AFRICA"}]},
        ]}
        patched, changes = patch_recipe(recipe)
        assert changes == []
        assert patched == recipe


def test_required_inputs_shape():
    """Sanity-check the constant matches the JSONB schema the API expects."""
    assert isinstance(REQUIRED_INPUTS, list)
    assert len(REQUIRED_INPUTS) == 1
    spec = REQUIRED_INPUTS[0]
    assert spec["key"] == "location"
    assert isinstance(spec["max_length"], int)
    assert isinstance(spec["required"], bool)
    assert spec["required"] is False  # blank input must be allowed (Africa fallback)


def test_outro_samples_are_lowercase():
    """patch_recipe lowercases sample before lookup, so set entries MUST be lowercase."""
    for entry in OUTRO_SAMPLES:
        assert entry == entry.lower(), f"OUTRO_SAMPLES entry {entry!r} is not lowercase"


def test_outro_template_contains_placeholder():
    """Substitution requires the literal '{subject}' placeholder."""
    assert "{subject}" in OUTRO_TEMPLATE


def test_new_sample_text_renders_as_africa_fallback():
    """Blank-input fallback must read 'Africa' (per product direction), not 'Morocco'."""
    assert "Africa" in NEW_SAMPLE_TEXT
    assert "Morocco" not in NEW_SAMPLE_TEXT
