"""Tests for font-registry.json — validates that all fonts are present and loadable."""

import json
import os
import string

import pytest

from app.pipeline.text_overlay import (
    _FONT_REGISTRY,
    FONTS_DIR,
    _registry_ass_name,
    _registry_font_path,
)

_REGISTRY_PATH = os.path.join(FONTS_DIR, "font-registry.json")
_ALLOWED_VIBES = {
    "viral_headlines",
    "clean_captions",
    "editorial",
    "handwritten",
    "script",
}
_EXTENDED_LATIN_SAMPLE = "áéíóúñãçüô"


class TestFontRegistryFile:
    """Validate font-registry.json structure and contents."""

    def test_registry_file_exists(self):
        assert os.path.exists(_REGISTRY_PATH), f"Missing: {_REGISTRY_PATH}"

    def test_registry_is_valid_json(self):
        with open(_REGISTRY_PATH) as f:
            data = json.load(f)
        assert "fonts" in data
        assert "style_defaults" in data

    def test_all_font_files_exist_on_disk(self):
        """Every font file referenced in the registry must exist."""
        fonts = _FONT_REGISTRY.get("fonts", {})
        assert len(fonts) > 0, "Registry has no fonts"
        for font_name, entry in fonts.items():
            path = os.path.join(FONTS_DIR, entry["file"])
            assert os.path.exists(path), f"Font file missing for '{font_name}': {entry['file']}"

    def test_all_font_files_are_loadable(self):
        """Every font file must be loadable by Pillow."""
        from PIL import ImageFont

        fonts = _FONT_REGISTRY.get("fonts", {})
        for font_name, entry in fonts.items():
            path = os.path.join(FONTS_DIR, entry["file"])
            font = ImageFont.truetype(path, 72)
            assert font is not None, f"Failed to load font '{font_name}'"

    def test_required_fields_present(self):
        """Each font entry has all required fields."""
        required = {"file", "ass_name", "weight", "category", "css_family"}
        fonts = _FONT_REGISTRY.get("fonts", {})
        for font_name, entry in fonts.items():
            missing = required - set(entry.keys())
            assert not missing, f"Font '{font_name}' missing fields: {missing}"

    def test_style_defaults_reference_valid_fonts(self):
        """Every style_default value must be a key in fonts."""
        fonts = _FONT_REGISTRY.get("fonts", {})
        defaults = _FONT_REGISTRY.get("style_defaults", {})
        for style, font_name in defaults.items():
            assert font_name in fonts, (
                f"style_defaults['{style}'] references '{font_name}' which is not in fonts"
            )

    def test_expected_font_count(self):
        """Registry has 40 active + 8 soft-deprecated fonts.

        Note: Outfit was previously deprecated (PR pre-gradient); PR #487 activated
        it as the OFL substitute for ZT Bros Oskon 90s, so the deprecated count is
        now 8 (not 9). Active count grew by 6 (Great Vibes, Satisfy, Patrick Hand,
        EB Garamond, Outfit, Plus Jakarta Sans), then by 1 more when
        Playfair Display Italic was added for accent_parity italic alternation
        in phrase sequences.
        """
        fonts = _FONT_REGISTRY.get("fonts", {})
        assert len(fonts) == 48
        assert sum(1 for entry in fonts.values() if entry.get("deprecated") is True) == 8
        assert sum(1 for entry in fonts.values() if entry.get("deprecated") is not True) == 40

    def test_expected_styles(self):
        """All 5 font styles should have defaults."""
        defaults = _FONT_REGISTRY.get("style_defaults", {})
        for style in ("display", "sans", "serif", "serif_italic", "script"):
            assert style in defaults, f"Missing style_default: {style}"

    def test_every_active_font_has_vibe(self):
        fonts = _FONT_REGISTRY.get("fonts", {})
        missing = [
            name
            for name, entry in fonts.items()
            if entry.get("deprecated") is not True and "vibe" not in entry
        ]
        assert not missing

    def test_vibe_values_in_allowed_set(self):
        fonts = _FONT_REGISTRY.get("fonts", {})
        invalid = {
            name: entry.get("vibe")
            for name, entry in fonts.items()
            if "vibe" in entry and entry.get("vibe") not in _ALLOWED_VIBES
        }
        assert invalid == {}

    def test_deprecated_field_is_bool_or_absent(self):
        fonts = _FONT_REGISTRY.get("fonts", {})
        invalid = {
            name: entry.get("deprecated")
            for name, entry in fonts.items()
            if "deprecated" in entry and entry.get("deprecated") is not True
        }
        assert invalid == {}

    def test_active_font_internal_family_matches_ass_name(self):
        # libass picks fonts from fontsdir by internal family name (nameID 1) or
        # typographic family (nameID 16). If the .ttf's internal family doesn't
        # match the registry's `ass_name`, libass silently falls back to a system
        # font and renders the wrong glyphs. Caught in prod after a variable-font
        # instancer left family="Montserrat Thin" on the static Regular cut.
        from fontTools.ttLib import TTFont

        fonts = _FONT_REGISTRY.get("fonts", {})
        mismatches = []
        for name, entry in fonts.items():
            if entry.get("deprecated") is True:
                continue
            ass_name = entry["ass_name"]
            path = os.path.join(FONTS_DIR, entry["file"])
            tt = TTFont(path)
            nt = tt["name"]
            internal_names = set()
            for nid in (1, 16):
                rec = nt.getName(nid, 3, 1, 1033) or nt.getName(nid, 1, 0, 0)
                if rec is not None:
                    internal_names.add(str(rec))
            if ass_name not in internal_names:
                mismatches.append(
                    f"{name}: ass_name='{ass_name}' not in internal names {internal_names}"
                )
        assert not mismatches, "\n".join(mismatches)

    def test_style_defaults_point_to_active_fonts(self):
        fonts = _FONT_REGISTRY.get("fonts", {})
        defaults = _FONT_REGISTRY.get("style_defaults", {})
        deprecated_defaults = {
            style: font_name
            for style, font_name in defaults.items()
            if fonts[font_name].get("deprecated") is True
        }
        assert deprecated_defaults == {}

    def test_settle_cycle_role_only_on_active_fonts(self):
        fonts = _FONT_REGISTRY.get("fonts", {})
        deprecated_settle_fonts = [
            name
            for name, entry in fonts.items()
            if entry.get("cycle_role") == "settle" and entry.get("deprecated") is True
        ]
        assert deprecated_settle_fonts == []

    def test_internal_ttf_name_matches_ass_name(self):
        """ASS font lookup depends on the registry name matching the TTF metadata."""
        ttlib = pytest.importorskip("fontTools.ttLib")
        fonts = _FONT_REGISTRY.get("fonts", {})
        for font_name, entry in fonts.items():
            path = os.path.join(FONTS_DIR, entry["file"])
            font = ttlib.TTFont(path)
            names = set()
            for record in font["name"].names:
                if record.nameID not in {1, 4, 16}:
                    continue
                try:
                    names.add(record.toUnicode().casefold())
                except UnicodeDecodeError:
                    continue
            assert entry["ass_name"].casefold() in names, (
                f"Font '{font_name}' ass_name={entry['ass_name']!r} did not match "
                f"TTF names {sorted(names)}"
            )

    def test_glyph_coverage_extended_latin(self):
        ttlib = pytest.importorskip("fontTools.ttLib")
        chars = string.printable[:95] + _EXTENDED_LATIN_SAMPLE
        fonts = _FONT_REGISTRY.get("fonts", {})
        for font_name, entry in fonts.items():
            if entry.get("deprecated") is True:
                continue
            path = os.path.join(FONTS_DIR, entry["file"])
            cmap = ttlib.TTFont(path).getBestCmap()
            missing = [char for char in chars if ord(char) not in cmap]
            assert missing == [], f"Font '{font_name}' missing glyphs: {missing}"


class TestRegistryLookups:
    """Test the Python helper functions that use the registry."""

    def test_registry_font_path_known_font(self):
        path = _registry_font_path("Playfair Display")
        assert path is not None
        assert path.endswith("PlayfairDisplay-Bold.ttf")
        assert os.path.exists(path)

    def test_registry_font_path_unknown_font(self):
        path = _registry_font_path("NonExistent Font 42")
        assert path is None

    def test_registry_ass_name_known_font(self):
        assert _registry_ass_name("Montserrat") == "Montserrat"
        assert _registry_ass_name("Space Grotesk") == "Space Grotesk"

    def test_registry_ass_name_unknown_font(self):
        # Falls back to the input name
        assert _registry_ass_name("Unknown") == "Unknown"

    @pytest.mark.parametrize(
        "font_name",
        [
            "Playfair Display",
            "Montserrat",
            "Space Grotesk",
            "DM Sans",
            "Instrument Serif",
            "Bodoni Moda",
            "Fraunces",
            "Space Mono",
            "Outfit",
        ],
    )
    def test_all_new_fonts_resolvable(self, font_name):
        """Each new font can be resolved to a valid path."""
        path = _registry_font_path(font_name)
        assert path is not None, f"Cannot resolve font: {font_name}"
        assert os.path.exists(path), f"Font file not found: {path}"
