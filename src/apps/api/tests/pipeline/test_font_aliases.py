"""Legacy font aliases burn their intended face in every renderer.

`TextElement` accepts the file-root names "Inter-Bold", "Inter-Regular",
"PlayfairDisplay-Bold" and "PlayfairDisplay-Regular" (guided-story narration
captions, slide-post rich text, web text presets). The web preview and the iOS
compiler draw them as Inter Bold / Inter Regular / Playfair Display Bold /
Playfair Display Regular, but the Skia resolver only knew registry keys, so
"Inter-Bold" silently burned the `display` style default (Playfair Display
Bold) — the preview showed Inter, the video showed Playfair. These guards pin
the alias → face mapping for Skia, Pillow, libass and the phone compiler, and
keep the web mirror in sync (renderer-parity invariant, #296 class).
"""

from __future__ import annotations

import io
import os
import re
import tempfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.agents._schemas.text_element import _LEGACY_FONT_ALIASES, TextElement
from app.pipeline import text_overlay
from app.pipeline import text_overlay_skia as tos
from app.pipeline.canvas import Canvas
from app.pipeline.font_aliases import LEGACY_FONT_ALIASES
from app.pipeline.generative_overlays import build_overlays_from_text_elements
from app.pipeline.portable_text_layout import compile_text_overlay

WEB_OVERLAY_CONSTANTS = (
    Path(__file__).resolve().parents[3] / "web" / "src" / "lib" / "overlay-constants.ts"
)

EXPECTED_FILES = {
    "Inter-Bold": "Inter-Bold.ttf",
    "Inter-Regular": "Inter-Regular.ttf",
    "PlayfairDisplay-Bold": "PlayfairDisplay-Bold.ttf",
    "PlayfairDisplay-Regular": "PlayfairDisplay-Regular.ttf",
}


def _registry_file(name: str) -> str:
    return text_overlay._FONT_REGISTRY["fonts"][name]["file"]


def test_every_alias_targets_a_registry_font_with_the_expected_file():
    assert set(LEGACY_FONT_ALIASES) == set(EXPECTED_FILES)
    for alias, target in LEGACY_FONT_ALIASES.items():
        assert _registry_file(target) == EXPECTED_FILES[alias]
        assert os.path.exists(os.path.join(text_overlay.FONTS_DIR, EXPECTED_FILES[alias]))


def test_text_element_allowlist_is_the_alias_map():
    assert _LEGACY_FONT_ALIASES == frozenset(LEGACY_FONT_ALIASES)


def test_web_preview_alias_map_matches_the_server():
    source = WEB_OVERLAY_CONSTANTS.read_text(encoding="utf-8")
    match = re.search(
        r"export const TEXT_ELEMENT_FONT_ALIASES: Record<string, string> = \{(.*?)\};",
        source,
        re.DOTALL,
    )
    assert match, f"TEXT_ELEMENT_FONT_ALIASES not found in {WEB_OVERLAY_CONSTANTS}"
    web = dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', match.group(1)))
    assert web == LEGACY_FONT_ALIASES


@pytest.mark.parametrize("alias", sorted(EXPECTED_FILES))
def test_text_element_alias_resolves_its_face_in_skia(alias):
    """The reported repro: TextElement → burn dict → Skia resolver."""
    element = TextElement(text="Hello", start_s=0, end_s=2, font_family=alias)
    (overlay,) = build_overlays_from_text_elements([element], video_duration_s=5)

    resolution = tos._resolve_typeface_for_overlay(overlay)

    assert resolution.file == EXPECTED_FILES[alias]
    assert resolution.name == LEGACY_FONT_ALIASES[alias]
    assert resolution.source == "font_family"
    assert resolution.fallback is False


@pytest.mark.parametrize("alias", sorted(EXPECTED_FILES))
def test_alias_resolves_its_face_in_pillow_and_libass(alias):
    target = LEGACY_FONT_ALIASES[alias]

    font = text_overlay._resolve_font_family(alias, 60)

    assert font is not None
    assert os.path.basename(font.path) == EXPECTED_FILES[alias]
    assert text_overlay._registry_font_path(alias) == text_overlay._registry_font_path(target)
    assert text_overlay._registry_ass_name(alias) == text_overlay._registry_ass_name(target)
    assert text_overlay._registry_ass_bold(alias) == text_overlay._registry_ass_bold(target)


@pytest.mark.parametrize("alias", sorted(EXPECTED_FILES))
def test_phone_compiler_ships_the_alias_face(alias):
    overlay = {
        "text": "Hello",
        "effect": "none",
        "font_family": alias,
        "text_size_px": 60,
        "position_y_frac": 0.5,
        "start_s": 0.0,
        "end_s": 2.0,
    }

    _layer, font_asset = compile_text_overlay(overlay, layer_id="t", canvas=Canvas(1080, 1920))

    assert font_asset.catalog_id == EXPECTED_FILES[alias]


def _skia_pixels(font_family: str) -> np.ndarray:
    overlay = {
        "text": "Hello Inter",
        "effect": "none",
        "font_family": font_family,
        "text_size_px": 90,
        "position_y_frac": 0.5,
        "text_color": "#FFFFFF",
        "start_s": 0.0,
        "end_s": 2.0,
    }
    image = tos._draw_frame(overlay, 1.0, 2.0)
    return np.asarray(Image.open(io.BytesIO(bytes(image.encodeToData()))).convert("RGBA"))


def _pillow_pixels(font_family: str) -> np.ndarray:
    overlay = {
        "text": "Hello Inter",
        "effect": "none",
        "font_family": font_family,
        "text_size_px": 90,
        "position_y_frac": 0.5,
        "text_color": "#FFFFFF",
        "start_s": 0.0,
        "end_s": 2.0,
    }
    with tempfile.TemporaryDirectory(prefix="font_alias_") as d:
        out = os.path.join(d, "p.png")
        text_overlay.render_overlays_at_time([overlay], 2.0, 1.0, out)
        return np.asarray(Image.open(out).convert("RGBA"))


@pytest.mark.parametrize("pixels", [_skia_pixels, _pillow_pixels], ids=["skia", "pillow"])
def test_both_renderers_burn_inter_bold_alias_as_inter_not_playfair(pixels):
    alias = pixels("Inter-Bold")

    assert np.array_equal(alias, pixels("Inter"))
    assert not np.array_equal(alias, pixels("Playfair Display"))
