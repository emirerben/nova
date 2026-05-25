"""Assemble the generative-edit "hero intro" overlay and inject it into a recipe.

The generative-edit flow writes its own opening text (no reference template, no song
lyrics). `IntroTextWriterAgent` produces the words and `OverlayFormatMatcherAgent`
produces the form (effect/position/colors). This module turns those two agent outputs
plus the hero slot's window into an overlay dict in the EXACT schema the existing Skia
renderer consumes (`text_overlay_skia`), then injects it into the hero slot's
`text_overlays`.

Deliberate design note (see plan "Decision 6"): generated overlays are injected
DIRECTLY into the recipe here, the same way `lyric_injector` injects lyric overlays.
They never pass through the `template_text` / `text_designer` agent schemas, so the
`VALID_EFFECTS` allowlist in `app/agents/_schemas/template_text.py` (which omits
`karaoke-line`) does NOT gate them. We still defensively coerce the effect to the
Skia-known set below so a drifted agent output can't produce an un-renderable overlay.

The vocab sets mirror the renderer's source of truth in `app/pipeline/text_overlay.py`
(`_FONT_SIZE_MAP`, `_POSITION_Y`) and the Skia draw dispatch. They are duplicated here
(not imported) to keep this module import-light — it must be unit-testable without
loading skia/PIL/fonts. If the renderer's vocab changes, update both.
"""

from __future__ import annotations

import structlog

from app.pipeline.word_timing import synthesize_word_timings

log = structlog.get_logger()

# Effects the Skia renderer can actually draw for a generated intro overlay.
# karaoke-line drives the word-by-word reveal; the rest are single-block effects.
_SKIA_EFFECTS = {"karaoke-line", "pop-in", "fade-in", "scale-up", "static"}
_DEFAULT_EFFECT = "static"

# Mirror of `text_overlay._FONT_SIZE_MAP` keys + `_POSITION_Y` keys + the anchor set.
_SIZE_CLASSES = {"small", "medium", "large", "xlarge", "xxlarge", "jumbo"}
_POSITIONS = {"center", "center-above", "center-label", "center-below", "top", "bottom"}
_ANCHORS = {"left", "right", "center"}

_DEFAULT_SIZE_CLASS = "jumbo"
_DEFAULT_POSITION = "center"
_DEFAULT_ANCHOR = "center"
_DEFAULT_TEXT_COLOR = "#FFFFFF"
_DEFAULT_HIGHLIGHT_COLOR = "#FFD24A"
_HEX_LEN = (4, 7)  # "#RGB" or "#RRGGBB"


def _coerce_hex(value: object, default: str) -> str:
    """Accept only a valid #RGB/#RRGGBB hex; anything else → default. Self-defending so
    a caller passing user-controlled colors can't push junk to the renderer."""
    v = str(value or "").strip()
    if v.startswith("#") and len(v) in _HEX_LEN:
        try:
            int(v[1:], 16)
            return v.upper()
        except ValueError:
            return default
    return default


def build_intro_overlay(
    text: str,
    *,
    effect: str,
    position: str = _DEFAULT_POSITION,
    size_class: str = _DEFAULT_SIZE_CLASS,
    text_color: str = _DEFAULT_TEXT_COLOR,
    highlight_color: str = _DEFAULT_HIGHLIGHT_COLOR,
    text_anchor: str = _DEFAULT_ANCHOR,
    start_s: float,
    end_s: float,
    beats: list[float] | None = None,
    highlight_word: str | None = None,
) -> dict | None:
    """Build a single hero-intro overlay dict in the Skia overlay schema.

    Returns None when there is nothing renderable (empty text or non-positive window)
    so the caller can skip injection and render footage without an intro — never crash.

    `effect`, `position`, `size_class`, `text_anchor` are defensively coerced to the
    renderer-known vocab; unknown values fall back to a safe default.
    """
    clean = (text or "").strip()
    if not clean:
        log.info("generative_overlay_skipped_empty_text")
        return None
    if float(end_s) - float(start_s) <= 0:
        log.info("generative_overlay_skipped_bad_window", start_s=start_s, end_s=end_s)
        return None

    effect = effect if effect in _SKIA_EFFECTS else _DEFAULT_EFFECT
    position = position if position in _POSITIONS else _DEFAULT_POSITION
    size_class = size_class if size_class in _SIZE_CLASSES else _DEFAULT_SIZE_CLASS
    text_anchor = text_anchor if text_anchor in _ANCHORS else _DEFAULT_ANCHOR

    overlay: dict = {
        "role": "generative_intro",
        "text": clean,
        "effect": effect,
        "start_s": round(float(start_s), 3),
        "end_s": round(float(end_s), 3),
        "position": position,
        "text_size": size_class,
        "text_anchor": text_anchor,
        "text_color": _coerce_hex(text_color, _DEFAULT_TEXT_COLOR),
        "highlight_color": _coerce_hex(highlight_color, _DEFAULT_HIGHLIGHT_COLOR),
        # subject substitution must never rewrite agent-authored intro text.
        "subject_substitute": False,
    }

    # Only the karaoke-line effect consumes per-word timings. Synthesize them from the
    # overlay window (even split, beat-snapped when a song is present).
    if effect == "karaoke-line":
        word_timings = synthesize_word_timings(
            clean.split(), 0.0, float(end_s) - float(start_s), beats=beats
        )
        if not word_timings:
            # No renderable words → downgrade to a static block rather than emit an
            # empty karaoke overlay (which the renderer would draw as plain text).
            overlay["effect"] = "static"
        else:
            overlay["word_timings"] = word_timings

    # highlight_word is informational metadata in v1 (the karaoke sweep, not a
    # per-word color, drives emphasis). Stored for future per-word-emphasis effects;
    # the renderer ignores unknown keys.
    if highlight_word:
        overlay["highlight_word"] = highlight_word

    return overlay


def inject_intro_overlay(recipe: dict, hero_slot_index: int, overlay: dict | None) -> dict:
    """Append the intro overlay to the hero slot's text_overlays. No-op on bad input.

    Never raises: a bad hero index or a None overlay leaves the recipe unmodified so a
    partial failure degrades to "edit without intro text", not a crashed render.
    """
    if overlay is None:
        return recipe
    slots = recipe.get("slots")
    if not isinstance(slots, list) or not slots:
        log.warning("generative_overlay_inject_no_slots")
        return recipe
    if not (0 <= hero_slot_index < len(slots)):
        log.warning(
            "generative_overlay_inject_hero_out_of_range",
            hero_slot_index=hero_slot_index,
            slot_count=len(slots),
        )
        return recipe

    slot = slots[hero_slot_index]
    arr = slot.get("text_overlays")
    if not isinstance(arr, list):
        arr = []
        slot["text_overlays"] = arr
    arr.append(overlay)
    return recipe
