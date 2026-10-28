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

"Needed" means letters, digits, punctuation, currency/math/other symbols and
leftover combining marks (see `needed_chars`); emoji are drawn by the emoji
path. A mapped glyph with no outline (Big Curls' ç ö ü) counts as missing.

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
def _font(font_file: str):
    from fontTools.ttLib import TTFont  # noqa: PLC0415

    return TTFont(os.path.join(FONTS_DIR, font_file), lazy=True)


@lru_cache(maxsize=128)
def _cmap(font_file: str) -> dict[int, str]:
    """Codepoint -> glyph name, minus a dotless ı that still has its dot.

    A mapped glyph is not always a correct one: ZT Bros Oskon 90s maps
    U+0131 (dotless ı) to an outline with a dot, so "ışığı" reads "işiği".
    A dotless i with at least as many contours as a dotted i is treated as
    missing so the whole-run fallback (and overlay_verify) catch it.
    """
    font = _font(font_file)
    cmap = dict(font.getBestCmap() or {})
    if _DOTLESS_I in cmap and ord("i") in cmap:
        try:
            glyphs = font.getGlyphSet()
            dotted = _contours(glyphs, cmap[ord("i")])
            if dotted >= 2 and _contours(glyphs, cmap[_DOTLESS_I]) >= dotted:
                cmap.pop(_DOTLESS_I)
        except Exception as exc:  # noqa: BLE001 - outline probe is best-effort
            log.warning("font_coverage_dotless_i_probe_failed", file=font_file, error=str(exc))
    return cmap


@lru_cache(maxsize=8192)
def _draws(font_file: str, codepoint: int) -> bool:
    """True when the font maps `codepoint` to a glyph with an outline.

    Big Curls maps ç Ç ö Ö ü Ü â î û (and é è ...) to EMPTY glyphs: a cmap-only
    check counts them as covered while Skia, Pillow and CoreText draw nothing
    ("Çok güzel" -> "ok g3el"). A blank outline counts as missing.
    """
    glyph = _cmap(font_file).get(codepoint)
    if glyph is None:
        return False
    try:
        from fontTools.pens.boundsPen import BoundsPen  # noqa: PLC0415

        glyph_set = _font(font_file).getGlyphSet()
        pen = BoundsPen(glyph_set)
        glyph_set[glyph].draw(pen)
    except Exception as exc:  # noqa: BLE001 - outline probe is best-effort
        log.warning("font_coverage_outline_probe_failed", file=font_file, error=str(exc))
        return True
    return pen.bounds is not None


_VARIATION_SELECTORS = range(0xFE00, 0xFE10)


def _is_emoji(ch: str) -> bool:
    import regex  # noqa: PLC0415 - Unicode emoji property

    return bool(regex.match(r"\p{Emoji}", ch))


def drop_color_emoji(text: str) -> str:
    """``text`` without the graphemes a color emoji face draws (KRI-1024).

    Flags (a regional-indicator pair), ZWJ sequences, skin tones, keycaps and
    VS16 emoji are shaped in the system emoji face on the phone (Apple Color
    Emoji, the only fallback its painter accepts) and in Noto Color Emoji by the
    cloud renderer, never in a bundled face. A font check reads the rest, so a
    text-style symbol a face lacks ("→", "©" with no VS16) still counts. For
    coverage checks only: the creator's words keep every emoji.
    """
    import regex  # noqa: PLC0415 - Unicode grapheme/emoji properties

    return "".join(
        grapheme
        for grapheme in regex.findall(r"\X", text or "")
        if not regex.search(r"\p{Emoji_Presentation}|\p{Emoji}️|⃣", grapheme)
    )


def needed_chars(text: str) -> str:
    """Characters a font must draw itself (NFC, deduped).

    Letters, digits, punctuation, currency and math symbols (₺ ° ×), other
    symbols that are not emoji, and combining marks NFC leaves standing (the
    U+0307 that locale-free lower-casing of İ produces). Emoji are drawn by the
    emoji path and variation selectors are not glyphs.
    """
    out = []
    for ch in dict.fromkeys(unicodedata.normalize("NFC", text or "")):
        if ch.isspace() or ord(ch) in _VARIATION_SELECTORS:
            continue
        category = unicodedata.category(ch)
        if (
            ch.isalnum()
            or category[0] == "P"
            or category in ("Sc", "Sm", "Mn")
            or (category == "So" and not _is_emoji(ch))
        ):
            out.append(ch)
    return "".join(out)


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
    font_file = resolved[1]["file"]
    try:
        _cmap(font_file)
    except Exception as exc:  # noqa: BLE001
        log.warning("font_coverage_cmap_failed", font_family=family, error=str(exc))
        return []
    return [ch for ch in chars if not _draws(font_file, ord(ch))]


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


def _span_texts(overlay: dict) -> tuple[str, list]:
    """(text drawn in the overlay family, spans) — inheriting spans included."""
    text = overlay.get("display_text") or overlay.get("text") or ""
    spans = overlay.get("spans")
    if not isinstance(spans, list):
        return text, []
    # Spans without their own font_family draw in the overlay family
    # (`_resolve_span_font` step 2), so their text must be covered by it too.
    inherited = "".join(
        str(span.get("text") or "")
        for span in spans
        if isinstance(span, dict) and not span.get("font_family")
    )
    return text + inherited, spans


def glyph_safe_overlay(overlay: dict, text: str | None = None) -> dict:
    """Copy of `overlay` with `font_family` (and span families) made glyph-safe.

    Returns the same dict object when nothing changes, so English renders stay
    byte-identical and callers can cheaply detect a swap.
    """
    own_text, spans = _span_texts(overlay)
    if text is None:
        text = own_text
    out = overlay
    family = overlay.get("font_family")
    if family:
        safe = glyph_safe_family(family, text)
        if safe != family:
            out = {**overlay, "font_family": safe}
    if spans:
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


def glyph_safe_overlays(overlays: list) -> list:
    """Make a whole burn glyph-safe with ONE face per requested family.

    Lyric lines and caption chunks are separate overlays that share a
    font_family; deciding per overlay would flip Satisfy -> Pacifico -> Satisfy
    line by line as each line happens to contain ş/ğ/ı. Every overlay that
    requests a family draws the face that covers the UNION of their texts.
    Returns the same list object when nothing changes (English stays
    byte-identical).
    """
    texts: dict[str, list[str]] = {}
    for overlay in overlays or []:
        if isinstance(overlay, dict) and overlay.get("font_family"):
            texts.setdefault(str(overlay["font_family"]), []).append(_span_texts(overlay)[0])
    if not texts:
        return overlays
    safe = {family: glyph_safe_family(family, " ".join(parts)) for family, parts in texts.items()}
    swaps = {family: face for family, face in safe.items() if face and face != family}
    if not swaps:
        return overlays
    return [
        {**overlay, "font_family": swaps[str(overlay["font_family"])]}
        if isinstance(overlay, dict) and str(overlay.get("font_family") or "") in swaps
        else overlay
        for overlay in overlays
    ]
