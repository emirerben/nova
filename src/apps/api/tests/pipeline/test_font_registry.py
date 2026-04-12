"""Tests for font-registry.json — validates that all fonts are present and loadable."""

import json
import os

import pytest

from app.pipeline.text_overlay import (
    FONTS_DIR,
    _FONT_REGISTRY,
    _registry_ass_name,
    _registry_font_path,
)

_REGISTRY_PATH = os.path.join(FONTS_DIR, "font-registry.json")


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
            assert os.path.exists(path), (
                f"Font file missing for '{font_name}': {entry['file']}"
            )

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
            assert not missing, (
                f"Font '{font_name}' missing fields: {missing}"
            )

    def test_style_defaults_reference_valid_fonts(self):
        """Every style_default value must be a key in fonts."""
        fonts = _FONT_REGISTRY.get("fonts", {})
        defaults = _FONT_REGISTRY.get("style_defaults", {})
        for style, font_name in defaults.items():
            assert font_name in fonts, (
                f"style_defaults['{style}'] references '{font_name}' "
                f"which is not in fonts"
            )

    def test_expected_font_count(self):
        """Registry should have at least 10 fonts (3 existing + 7 new)."""
        fonts = _FONT_REGISTRY.get("fonts", {})
        assert len(fonts) >= 10

    def test_expected_styles(self):
        """All 5 font styles should have defaults."""
        defaults = _FONT_REGISTRY.get("style_defaults", {})
        for style in ("display", "sans", "serif", "serif_italic", "script"):
            assert style in defaults, f"Missing style_default: {style}"


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

    @pytest.mark.parametrize("font_name", [
        "Playfair Display",
        "Montserrat",
        "Space Grotesk",
        "DM Sans",
        "Instrument Serif",
        "Bodoni Moda",
        "Fraunces",
        "Space Mono",
        "Outfit",
    ])
    def test_all_new_fonts_resolvable(self, font_name):
        """Each new font can be resolved to a valid path."""
        path = _registry_font_path(font_name)
        assert path is not None, f"Cannot resolve font: {font_name}"
        assert os.path.exists(path), f"Font file not found: {path}"
