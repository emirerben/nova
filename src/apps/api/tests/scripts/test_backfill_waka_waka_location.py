"""Unit tests for the patch_recipe helper in scripts/backfill_waka_waka_location.py.

Covers the pure functions only — DB interaction is exercised manually via
`--apply` against a dev DB and noted in the script docstring.
"""
from __future__ import annotations

from scripts.backfill_waka_waka_location import (
    NEW_SAMPLE_TEXT,
    OUTRO_PREFIX,
    OUTRO_TEMPLATE,
    REQUIRED_INPUTS,
    UNLOCK_SLOT_TYPES,
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


def test_outro_prefix_is_lowercase_with_trailing_space():
    """patch_recipe lowercases sample before lookup, so the prefix MUST be lowercase.
    Trailing space is critical — without it, 'shukrananything' would match."""
    assert OUTRO_PREFIX == OUTRO_PREFIX.lower()
    assert OUTRO_PREFIX.endswith(" "), "OUTRO_PREFIX must end with space"


def test_outro_template_contains_placeholder():
    """Substitution requires the literal '{subject}' placeholder."""
    assert "{subject}" in OUTRO_TEMPLATE


def test_new_sample_text_renders_as_africa_fallback():
    """Blank-input fallback must read 'Africa' (per product direction), not 'Morocco'."""
    assert "Africa" in NEW_SAMPLE_TEXT
    assert "Morocco" not in NEW_SAMPLE_TEXT


def test_unlock_slot_types_targets_hook_and_face():
    """Hook + face are the opening beats — those are the ones that must
    accept user clips. Broll slots are sometimes intentionally locked to
    transition footage; don't touch them."""
    assert "hook" in UNLOCK_SLOT_TYPES
    assert "face" in UNLOCK_SLOT_TYPES
    assert "broll" not in UNLOCK_SLOT_TYPES
    assert "outro" not in UNLOCK_SLOT_TYPES


class TestPatchRecipeUnlock:
    """Covers the locked-hook-slot unlock branch (PR #111's manual TODO)."""

    def test_unlocks_locked_hook_slots(self):
        """Pre-backfill Morocco recipe: slots 1+2 both `locked: true, slot_type: hook`."""
        recipe = {"slots": [
            {"position": 1, "slot_type": "hook", "locked": True,
             "source_start_s": 0.0, "source_end_s": 1.3,
             "target_duration_s": 1.3, "text_overlays": []},
            {"position": 2, "slot_type": "hook", "locked": True,
             "source_start_s": 1.3, "source_end_s": 2.5,
             "target_duration_s": 1.2, "text_overlays": []},
            {"position": 3, "slot_type": "broll", "locked": False,
             "target_duration_s": 2.4, "text_overlays": []},
        ]}
        patched, changes = patch_recipe(recipe)
        unlock_changes = [c for c in changes if c[0] == "unlock"]
        assert len(unlock_changes) == 2
        assert patched["slots"][0]["locked"] is False
        assert patched["slots"][1]["locked"] is False
        # Other slot fields preserved (source_start_s etc. become dead but
        # we leave them — minimal diff, renderer ignores them when unlocked).
        assert patched["slots"][0]["source_start_s"] == 0.0
        assert patched["slots"][0]["target_duration_s"] == 1.3
        assert patched["slots"][2]["locked"] is False  # already unlocked, untouched

    def test_unlocks_face_slots_too(self):
        """Face-typed locked slots (rare but possible in face-intro templates)."""
        recipe = {"slots": [
            {"position": 1, "slot_type": "face", "locked": True, "text_overlays": []},
        ]}
        patched, changes = patch_recipe(recipe)
        assert len([c for c in changes if c[0] == "unlock"]) == 1
        assert patched["slots"][0]["locked"] is False

    def test_does_not_unlock_locked_broll(self):
        """Some templates intentionally lock broll slots for transition footage.
        We only touch opening beats (hook/face), never broll/outro/reaction."""
        recipe = {"slots": [
            {"position": 3, "slot_type": "broll", "locked": True, "text_overlays": []},
            {"position": 4, "slot_type": "outro", "locked": True, "text_overlays": []},
            {"position": 5, "slot_type": "reaction", "locked": True, "text_overlays": []},
        ]}
        patched, changes = patch_recipe(recipe)
        assert [c for c in changes if c[0] == "unlock"] == []
        for slot in patched["slots"]:
            assert slot["locked"] is True

    def test_idempotent_on_already_unlocked(self):
        """Rerunning the backfill against a recipe whose hook slots are
        already unlocked is a no-op."""
        recipe = {"slots": [
            {"position": 1, "slot_type": "hook", "locked": False, "text_overlays": []},
            {"position": 2, "slot_type": "hook", "locked": False, "text_overlays": []},
        ]}
        patched, changes = patch_recipe(recipe)
        assert [c for c in changes if c[0] == "unlock"] == []
        assert patched == recipe

    def test_missing_locked_field_treated_as_unlocked(self):
        """Recipes without an explicit `locked` field default to unlocked.
        Don't add a redundant `locked: false` — minimal diff."""
        recipe = {"slots": [
            {"position": 1, "slot_type": "hook", "text_overlays": []},
        ]}
        patched, changes = patch_recipe(recipe)
        assert [c for c in changes if c[0] == "unlock"] == []
        assert "locked" not in patched["slots"][0]

    def test_combined_outro_tag_and_slot_unlock(self):
        """Realistic prod-recipe scenario: both issues fixed in one pass."""
        recipe = {"slots": [
            {"position": 1, "slot_type": "hook", "locked": True, "text_overlays": []},
            {"position": 2, "slot_type": "hook", "locked": True, "text_overlays": []},
            {"position": 24, "slot_type": "outro", "locked": False, "text_overlays": [{
                "role": "reaction", "text": "", "sample_text": "shukran Morocco!",
                "effect": "bounce",
            }]},
        ]}
        patched, changes = patch_recipe(recipe)
        unlocks = [c for c in changes if c[0] == "unlock"]
        outros = [c for c in changes if c[0] == "outro"]
        assert len(unlocks) == 2
        assert len(outros) == 1
        # Slots 1+2 unlocked
        assert patched["slots"][0]["locked"] is False
        assert patched["slots"][1]["locked"] is False
        # Outro overlay tagged + renamed
        outro_ov = patched["slots"][2]["text_overlays"][0]
        assert outro_ov["sample_text"] == NEW_SAMPLE_TEXT
        assert outro_ov["subject_template"] == OUTRO_TEMPLATE


class TestOutroPunctuationDrift:
    """The outro literal can drift between Gemini analysis runs (different
    punctuation, casing, country). Prefix match must survive all realistic
    variants so reanalysis doesn't silently break the backfill."""

    def _run(self, sample_text: str) -> tuple[int, dict]:
        recipe = {"slots": [{"text_overlays": [
            {"role": "reaction", "text": "", "sample_text": sample_text,
             "effect": "bounce"},
        ]}]}
        patched, changes = patch_recipe(recipe)
        return len([c for c in changes if c[0] == "outro"]), \
            patched["slots"][0]["text_overlays"][0]

    def test_no_exclamation(self):
        """Gemini drops the bang sometimes."""
        n, ov = self._run("shukran Morocco")
        assert n == 1
        assert ov["sample_text"] == NEW_SAMPLE_TEXT
        assert ov["subject_template"] == OUTRO_TEMPLATE

    def test_capital_S_shukran(self):
        """Gemini title-cases the Arabic loanword."""
        n, _ = self._run("Shukran Morocco!")
        assert n == 1

    def test_trailing_whitespace(self):
        """Recipe authors fat-finger a trailing space."""
        n, _ = self._run("shukran Morocco! ")
        assert n == 1

    def test_period_instead_of_bang(self):
        """Punctuation alternative."""
        n, _ = self._run("shukran Morocco.")
        assert n == 1

    def test_different_country(self):
        """Gemini swapped Morocco for whatever location it inferred from
        the song's lyrics or the template author's tweak. We don't care
        what came after 'shukran ' — we're overwriting it anyway."""
        n, ov = self._run("shukran Tunisia!")
        assert n == 1
        assert ov["sample_text"] == NEW_SAMPLE_TEXT  # always Africa fallback

    def test_bare_shukran_does_not_match(self):
        """No trailing word — refuse to tag. Prevents catastrophic over-match
        if a future template uses just 'shukran' as a standalone overlay."""
        n, _ = self._run("shukran")
        assert n == 0

    def test_shukran_with_only_trailing_space_does_not_match(self):
        n, _ = self._run("shukran ")
        assert n == 0

    def test_unrelated_overlay_not_matched(self):
        """Don't catch things that merely contain 'shukran' mid-sentence."""
        n, _ = self._run("we say shukran every day")
        assert n == 0

    def test_already_correctly_tagged_is_idempotent(self):
        """If reanalysis happened AFTER a successful backfill and emitted
        the SAME sample_text we wrote ('shukran Africa!') plus our
        subject_template, treat as no-op."""
        recipe = {"slots": [{"text_overlays": [{
            "sample_text": NEW_SAMPLE_TEXT,
            "subject_template": OUTRO_TEMPLATE,
        }]}]}
        _, changes = patch_recipe(recipe)
        assert [c for c in changes if c[0] == "outro"] == []
