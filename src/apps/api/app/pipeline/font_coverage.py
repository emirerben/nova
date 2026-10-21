"""Glyph coverage + whole-run font fallback, shared by every text renderer (KRI-785).

Several bundled display faces lack Turkish letters (Alte Haas Grotesk,
Permanent Marker, Satisfy: ğ Ğ ş Ş İ; Big Curls also ı; Rascal all twelve).
Skia draws a tofu box for a missing glyph and libass/Pillow drop or box it,
so "Güneş ışığı" in Satisfy shipped as "Gün?? ???" with no signal.

The rule, applied identically by the Skia renderer, the Pillow/libass path,
`overlay_verify` and (mirrored in Swift) the iOS compiler:

  If the chosen registry font lacks any glyph the text needs, the WHOLE text
  run is set in one stylistically close registry font that covers it — never
  per letter (a Satisfy word with one Pacifico "ş" looks broken too).

Fallback order: `glyph-fallbacks.json[family]` → same-category, non-deprecated
registry font with the closest weight → the `display` style default. When no
registry font covers the text (CJK, Arabic), the family is returned unchanged
so the existing fail-fast gates (`assert_lyric_glyphs`, the intro-cluster
glyph gate) still fire.

Only letters, digits and punctuation count as "needed": emoji are drawn by
the emoji path, and combining marks ride on their base letter.

Dependency-free beyond fontTools + the registry JSON (no skia, no PIL) so the
Pillow path, intro_cluster and the structural evals can all import it.
"""

from __future__ import annotations

import json
import os
import unicodedata
from functools import lru_cache

import structlog

from app.pipeline.font_aliases import registry_font_name

log = structlog.get_logger()

FONTS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "assets", "fonts"))
_REGISTRY_PATH = os.path.join(FONTS_DIR, "font-registry.json")
_FALLBACKS_PATH = os.path.join(FONTS_DIR, "glyph-fallbacks.json")


@lru_cache(maxsize=1)
def _registry() -> dict:
    try:
        with open(_REGISTRY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001 - coverage must fail safe (no swap)
        log.warning("font_coverage_registry_load_failed", error=str(exc))
        return {"fonts": {}, "style_defaults": {}}


@lru_cache(maxsize=1)
def _explicit_fallbacks() -> dict[str, str]:
    try:
        with open(_FALLBACKS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:  # noqa: BLE001
        log.warning("font_coverage_fallbacks_load_failed", error=str(exc))
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, str)}


_DOTLESS_I = 0x0131


def _contours(glyph_set, glyph_name: str) -> int:
    from fontTools.pens.recordingPen import RecordingPen  # noqa: PLC0415

    pen = RecordingPen()
    glyph_set[glyph_name].draw(pen)
    return sum(1 for op, _args in pen.value if op in ("closePath", "endPath"))


@lru_cache(maxsize=128)
def _cmap(font_file: str) -> frozenset[int]:
    """Codepoints the font really draws.

    A mapped glyph is not always a correct one: ZT Bros Oskon 90s maps
    U+0131 (dotless ı) to an outline with a dot, so "ışığı" reads "işiği".
    A dotless i with at least as many contours as a dotted i is treated as
    missing so the whole-run fallback (and overlay_verify) catch it.
    """
    from fontTools.ttLib import TTFont  # noqa: PLC0415

    font = TTFont(os.path.join(FONTS_DIR, font_file), lazy=True)
    try:
        cmap = dict(font.getBestCmap() or {})
        if _DOTLESS_I in cmap and ord("i") in cmap:
            try:
                glyphs = font.getGlyphSet()
                dotted = _contours(glyphs, cmap[ord("i")])
                if dotted >= 2 and _contours(glyphs, cmap[_DOTLESS_I]) >= dotted:
                    cmap.pop(_DOTLESS_I)
            except Exception as exc:  # noqa: BLE001 - outline probe is best-effort
                log.warning("font_coverage_dotless_i_probe_failed", file=font_file, error=str(exc))
        return frozenset(cmap.keys())
    finally:
        font.close()


def needed_chars(text: str) -> str:
    """Characters a font must draw itself: letters, digits, punctuation (deduped)."""
    return "".join(
        ch
        for ch in dict.fromkeys(text or "")
        if ch.isalnum() or unicodedata.category(ch).startswith("P")
    )


def _entry(family: str | None) -> tuple[str, dict] | None:
    if not family:
        return None
    name = registry_font_name(str(family))
    entry = _registry().get("fonts", {}).get(name)
    return (name, entry) if entry else None


def missing_glyphs(family: str | None, text: str) -> list[str]:
    """Needed characters of `text` absent from the registry font `family`.

    Unknown families and unreadable files report nothing missing: this helper
    only ever narrows to a fallback, it never invents a failure for a face it
    cannot inspect.
    """
    resolved = _entry(family)
    chars = needed_chars(text)
    if resolved is None or not chars:
        return []
    try:
        cmap = _cmap(resolved[1]["file"])
    except Exception as exc:  # noqa: BLE001
        log.warning("font_coverage_cmap_failed", font_family=family, error=str(exc))
        return []
    return [ch for ch in chars if ord(ch) not in cmap]


def font_covers_text(family: str | None, text: str) -> bool:
    return not missing_glyphs(family, text)


def _candidates(name: str, entry: dict) -> list[str]:
    fonts = _registry().get("fonts", {})
    out: list[str] = []
    explicit = _explicit_fallbacks().get(name)
    if explicit:
        out.append(explicit)
    category = entry.get("category")
    weight = int(entry.get("weight", 400))
    same_category = [
        (abs(int(other.get("weight", 400)) - weight), idx, other_name)
        for idx, (other_name, other) in enumerate(fonts.items())
        if other_name != name and other.get("category") == category
    ]
    out.extend(n for _d, _i, n in sorted(same_category))
    display = _registry().get("style_defaults", {}).get("display")
    if display:
        out.append(display)
    return out


@lru_cache(maxsize=1024)
def glyph_safe_family(family: str | None, text: str) -> str | None:
    """`family` if it covers `text`, else the whole-run fallback (see module doc)."""
    resolved = _entry(family)
    if resolved is None or font_covers_text(family, text):
        return family
    name, entry = resolved
    fonts = _registry().get("fonts", {})
    for candidate in _candidates(name, entry):
        cand_entry = fonts.get(candidate)
        if not cand_entry or cand_entry.get("deprecated") is True:
            continue
        if font_covers_text(candidate, text):
            log.info(
                "font_glyph_fallback",
                font_family=name,
                fallback=candidate,
                missing="".join(missing_glyphs(name, text)),
            )
            return candidate
    return family


def glyph_safe_overlay(overlay: dict, text: str | None = None) -> dict:
    """Copy of `overlay` with `font_family` (and span families) made glyph-safe.

    Returns the same dict object when nothing changes, so English renders stay
    byte-identical and callers can cheaply detect a swap.
    """
    if text is None:
        text = overlay.get("display_text") or overlay.get("text") or ""
    out = overlay
    family = overlay.get("font_family")
    if family:
        safe = glyph_safe_family(family, text)
        if safe != family:
            out = {**overlay, "font_family": safe}
    spans = overlay.get("spans")
    if isinstance(spans, list):
        new_spans = []
        changed = False
        for span in spans:
            if isinstance(span, dict) and span.get("font_family"):
                safe = glyph_safe_family(span["font_family"], str(span.get("text") or ""))
                if safe != span["font_family"]:
                    span = {**span, "font_family": safe}
                    changed = True
            new_spans.append(span)
        if changed:
            out = {**out, "spans": new_spans}
    return out
