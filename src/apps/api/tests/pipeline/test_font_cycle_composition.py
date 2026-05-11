"""Regression guard for the font-cycle font set used by Dimples slot 5.

The BRAZIL title in slot 5 of the Dimples Passport Travel Vlog template
flashes through a list of "contrast" fonts during its font-cycle effect.
The cycle reads as visually clean when every flash lands on a heavy,
high-stroke-weight letterform (block sans, slab serif, bold modern
serif). It reads as "pixelated/glitchy" when a flash lands on a thin
script/marker face (Pacifico, Permanent Marker) because those fonts
have thin uneven strokes and connected letterforms that, at 170px on a
1080-wide frame, produce visible irregular edges that look like
compression artifacts even when the encoder is doing its job.

This module pins the cycle composition so a future registry edit that
silently re-introduces a script/marker font (or removes a heavy
contrast font) trips the test before it ships.
"""
from __future__ import annotations

from app.pipeline.text_overlay import _CYCLE_CONTRAST_NAMES

# The cycle SHOULD include these heavy-weight fonts. Removing any of
# them shrinks variety below what the BRAZIL hook needs to read as
# "flickering through multiple looks."
_REQUIRED_CYCLE_FONTS = frozenset({
    "Montserrat",
    "Instrument Serif",
    "Bodoni Moda",
    "Fraunces",
})

# The cycle MUST NOT include these thin script/marker fonts. They
# produced visible "pixel pixel" letter rendering in Dimples slot 5 that
# the user flagged as glitchy even after the encoder banding fix
# (PR #100 follow-up, 2026-05-11).
_BANNED_CYCLE_FONTS = frozenset({
    "Pacifico",
    "Permanent Marker",
})


def test_font_cycle_contains_required_heavy_fonts() -> None:
    """Cycle must contain every heavy-weight contrast font. Removing any
    one shrinks BRAZIL hook variety and breaks the visual cadence."""
    cycle = set(_CYCLE_CONTRAST_NAMES)
    missing = _REQUIRED_CYCLE_FONTS - cycle
    assert not missing, (
        f"Required heavy-weight cycle fonts are missing from the registry's "
        f"cycle_role=contrast set: {sorted(missing)}. Currently in cycle: "
        f"{sorted(cycle)}. Re-add them with cycle_role='contrast' in "
        f"assets/fonts/font-registry.json."
    )


def test_font_cycle_excludes_thin_script_fonts() -> None:
    """Cycle must NOT contain thin script/marker fonts. They render as
    visually 'pixelated' to users at slot-5's 170px text size and break
    the perceived sharpness of the flash effect, even when the encoder
    quality is good."""
    cycle = set(_CYCLE_CONTRAST_NAMES)
    banned_present = _BANNED_CYCLE_FONTS & cycle
    assert not banned_present, (
        f"Banned thin script/marker fonts are back in the cycle: "
        f"{sorted(banned_present)}. These render as 'pixelated' on slot 5 "
        f"BRAZIL title (Dimples Passport). Set their cycle_role to "
        f"'decorative' (or anything other than 'contrast') in "
        f"assets/fonts/font-registry.json. The fonts remain available as "
        f"explicit font_family choices on overlays — they're only banned "
        f"from the rapid cycle pool."
    )


def test_font_cycle_size_within_sane_bounds() -> None:
    """Sanity: cycle should contain enough fonts to feel like a cycle
    (>=3) but not so many that adjacent flash frames render the same
    font (>~8)."""
    n = len(_CYCLE_CONTRAST_NAMES)
    assert 3 <= n <= 8, (
        f"Font cycle has {n} contrast fonts. Expected 3-8. Adjust "
        f"cycle_role assignments in assets/fonts/font-registry.json."
    )
