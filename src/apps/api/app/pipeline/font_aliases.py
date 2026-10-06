"""Legacy file-root font names → font-registry keys.

TextElement rows still carry the file-root names the burn dict used before the
font registry existed: guided-story narration captions and v<3 titles
("Inter-Bold"), slide-post rich text (`DEFAULT_SLIDE_TEXT_FONT`), web text
presets, visual-block text cards ("PlayfairDisplay-Bold"). The web preview
(`TEXT_ELEMENT_FONT_ALIASES` in `src/apps/web/src/lib/overlay-constants.ts`)
and the iOS compiler already draw these as the faces below. Every server
renderer (Skia, Pillow PNG, libass) and the phone compiler must resolve them
the same way — an unresolved alias silently burns the `display` style default
(Playfair Display Bold). Dependency-free so `text_element` can import it at
module scope.
"""

from __future__ import annotations

LEGACY_FONT_ALIASES: dict[str, str] = {
    "PlayfairDisplay-Bold": "Playfair Display",
    "PlayfairDisplay-Regular": "Playfair Display Regular",
    "Inter-Bold": "Inter",
    "Inter-Regular": "Inter Regular",
}


def registry_font_name(name: str) -> str:
    """Return the font-registry key for `name` (identity for non-aliases)."""
    return LEGACY_FONT_ALIASES.get(name, name)
