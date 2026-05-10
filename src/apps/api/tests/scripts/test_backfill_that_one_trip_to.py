"""Unit tests for the patch_recipe helper in scripts/backfill_that_one_trip_to.py.

Covers the pure functions only — DB interaction is exercised manually via
`--apply` against a dev DB and noted in the plan's verification checklist.
"""
from __future__ import annotations

from scripts.backfill_that_one_trip_to import (
    FRAGMENT_MAP,
    REQUIRED_INPUTS,
    TYPEWRITER_SUFFIXES,
    _overlay_sample,
    _typewriter_match,
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
        # Fragment change shape: ("fragment", slot_idx, ov_idx, sample, old, new)
        kind, _, _, _, old, new = changes[0]
        assert kind == "fragment"
        assert old == "second_half"
        assert new == "first_half"
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


class TestTypewriterMatch:
    """_typewriter_match detects typewriter sentences ending in 'lon'/'london'."""

    def test_full_london_suffix(self):
        assert _typewriter_match("that one trip to london") == ("that one trip to ", None)

    def test_partial_lon_suffix(self):
        assert _typewriter_match("that one trip to lon") == ("that one trip to ", 3)

    def test_case_insensitive(self):
        # Suffix match is case-insensitive but the prefix preserves source casing.
        assert _typewriter_match("That One Trip To LONDON") == ("That One Trip To ", None)

    def test_bare_word_returns_none(self):
        """Bare 'london'/'lon' is the FRAGMENT_MAP path, not typewriter."""
        assert _typewriter_match("london") is None
        assert _typewriter_match("lon") is None

    def test_no_space_before_suffix_returns_none(self):
        """'tolondon' shouldn't match — needs the space delimiter."""
        assert _typewriter_match("tolondon") is None
        assert _typewriter_match("balloon") is None

    def test_unrelated_text_returns_none(self):
        assert _typewriter_match("Welcome to PERU") is None
        assert _typewriter_match("hello world") is None

    def test_longest_suffix_wins(self):
        """'london' must be checked before 'lon' so the full word wins."""
        # If 'lon' matched first, "...london" would resolve to chars=3 which is wrong.
        assert _typewriter_match("trip to london") == ("trip to ", None)

    def test_typewriter_suffixes_ordered_longest_first(self):
        """Sanity: list order matters — longest suffix must be first."""
        suffixes = [s for s, _ in TYPEWRITER_SUFFIXES]
        assert suffixes == sorted(suffixes, key=len, reverse=True)


class TestPatchRecipeTypewriter:
    """patch_recipe handles the full prod recipe shape — typewriter slots + reveal slots."""

    def test_tags_typewriter_and_reveal_slots(self):
        """Mirrors the actual prod recipe: 6 typewriter slots + 5 reveal slots."""
        recipe = {"slots": [
            # Typewriter buildup (slots 0-3 contain no city — should not be tagged)
            {"text_overlays": [{"text": "that"}]},
            {"text_overlays": [{"text": "that one"}]},
            {"text_overlays": [{"text": "that one trip"}]},
            {"text_overlays": [{"text": "that one trip to"}]},
            # Slot 4: partial reveal
            {"text_overlays": [{"text": "that one trip to lon"}]},
            # Slot 5: full sentence
            {"text_overlays": [{"text": "that one trip to london"}]},
            # Slots 6-10: bare-word reveals
            {"text_overlays": [{"text": "London"}]},
            {"text_overlays": [{"text": "London"}]},
        ]}
        patched, changes = patch_recipe(recipe)

        # 4 changes total: 2 typewriter (slots 4, 5) + 2 fragments (slots 6, 7)
        assert len(changes) == 4

        # Slots 0-3: untouched (no city in their text)
        for i in range(4):
            ov = patched["slots"][i]["text_overlays"][0]
            assert "subject_template" not in ov
            assert "subject_part" not in ov

        # Slot 4: partial typewriter
        ov4 = patched["slots"][4]["text_overlays"][0]
        assert ov4["subject_template"] == "that one trip to {subject}"
        assert ov4["subject_chars"] == 3

        # Slot 5: full sentence
        ov5 = patched["slots"][5]["text_overlays"][0]
        assert ov5["subject_template"] == "that one trip to {subject}"
        assert ov5["subject_chars"] is None

        # Slots 6-7: bare 'London' fragment → subject_part="full"
        for i in (6, 7):
            ov = patched["slots"][i]["text_overlays"][0]
            assert ov["subject_part"] == "full"
            assert "subject_template" not in ov

    def test_idempotent_on_already_tagged_typewriter(self):
        recipe = {"slots": [{"text_overlays": [{
            "text": "that one trip to london",
            "subject_template": "that one trip to {subject}",
            "subject_chars": None,
        }]}]}
        _, changes = patch_recipe(recipe)
        assert changes == []

    def test_re_tags_typewriter_with_wrong_chars(self):
        """If subject_chars is wrong (operator hand-edit), backfill corrects it."""
        recipe = {"slots": [{"text_overlays": [{
            "text": "that one trip to lon",
            "subject_template": "that one trip to {subject}",
            "subject_chars": 5,  # wrong — should be 3
        }]}]}
        patched, changes = patch_recipe(recipe)
        assert len(changes) == 1
        assert changes[0][0] == "typewriter"
        assert patched["slots"][0]["text_overlays"][0]["subject_chars"] == 3

    def test_typewriter_sample_text_takes_precedence_over_text(self):
        """If sample_text is set, it wins over text (mirrors _overlay_sample)."""
        recipe = {"slots": [{"text_overlays": [{
            "sample_text": "that one trip to london",
            "text": "ignored",
        }]}]}
        _, changes = patch_recipe(recipe)
        assert len(changes) == 1
        assert changes[0][0] == "typewriter"
