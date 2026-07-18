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

import math
from typing import TYPE_CHECKING

import structlog

from app.pipeline.word_timing import synthesize_word_timings

if TYPE_CHECKING:
    from app.agents._schemas.text_element import TextElement

log = structlog.get_logger()

# Effects the Skia renderer can actually draw for a generated intro overlay.
# karaoke-line drives the word-by-word reveal; the rest are single-block effects
# dispatched by `_draw_with_animation` in text_overlay_skia.py. This mirrors that
# dispatch so a curated style set's effect (typewriter / stream-in / pop-in / …)
# survives instead of being flattened to `static`. font-cycle/glitch are excluded:
# font-cycle needs a `cycle_fonts` list, glitch has no Skia draw path.
_SKIA_EFFECTS = {
    "karaoke-line",
    "pop-in",
    "fade-in",
    "scale-up",
    "static",
    "typewriter",
    "stream-in",
    "bounce",
    "slide-up",
    "slide-in",
}
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


# ── Parity-gated style-field resolvers (T11, D9/D17) ─────────────────────────
#
# Pure, import-light helpers shared by the Skia renderer and the parity
# contract tests. Each has an EXACT TS mirror in
# src/apps/web/src/lib/overlay-layout.ts, locked by a shared JSON fixture in
# tests/fixtures/text-element-parity/ (see
# tests/pipeline/test_text_element_parity_contract.py).

# Mirrors LETTER_SPACING_* / LINE_SPACING_* in text_element.py + overlay-layout.ts.
_LETTER_SPACING_MIN_EM = -0.05
_LETTER_SPACING_MAX_EM = 0.5
_LINE_SPACING_MIN = 0.5
_LINE_SPACING_MAX = 3.0
_MAX_WIDTH_FRAC_MIN = 0.2
_MAX_WIDTH_FRAC_MAX = 1.0
DEFAULT_MAX_WIDTH_FRAC = 0.9
# Renderer default line-height multiplier (Skia _LINE_SPACING / CSS preview).
DEFAULT_LINE_SPACING = 1.15


def resolve_letter_spacing_em(value: object) -> float:
    """Clamped letter-spacing in em; 0.0 (no tracking) for absent/invalid.
    TS mirror: resolveLetterSpacingEm (overlay-layout.ts)."""
    if value is None:
        return 0.0
    try:
        return max(_LETTER_SPACING_MIN_EM, min(_LETTER_SPACING_MAX_EM, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def resolve_letter_spacing_px(value: object, font_size_px: float) -> float:
    """em → px at the FINAL drawn font size (both renderers scale tracking with
    the size the text actually renders at, including shrink-to-fit).
    TS mirror: resolveLetterSpacingPx (overlay-layout.ts)."""
    return resolve_letter_spacing_em(value) * float(font_size_px)


def resolve_line_spacing(value: object) -> float:
    """Clamped line-height multiplier; DEFAULT_LINE_SPACING for absent/invalid.
    TS mirror: resolveLineSpacing (overlay-layout.ts)."""
    if value is None:
        return DEFAULT_LINE_SPACING
    try:
        return max(_LINE_SPACING_MIN, min(_LINE_SPACING_MAX, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_LINE_SPACING


def resolve_max_width_frac(value: object) -> float:
    """Clamped max wrap width as a fraction of frame width; renderer default
    (0.9) for absent/invalid. TS mirror: resolveMaxWidthFrac
    (overlay-layout.ts)."""
    if value is None:
        return DEFAULT_MAX_WIDTH_FRAC
    try:
        return max(_MAX_WIDTH_FRAC_MIN, min(_MAX_WIDTH_FRAC_MAX, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_MAX_WIDTH_FRAC


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
    font_family: str | None = None,
    stroke_width: int | None = None,
    shadow_enabled: bool | None = None,
    text_size_px: int | None = None,
    position_x_frac: float | None = None,
    position_y_frac: float | None = None,
    rotation_deg: float | None = None,
    letter_spacing: float | None = None,
    line_spacing: float | None = None,
    max_width_frac: float | None = None,
    behind_subject: bool = False,
) -> dict | None:
    """Build a single hero-intro overlay dict in the Skia overlay schema.

    Returns None when there is nothing renderable (empty text or non-positive window)
    so the caller can skip injection and render footage without an intro — never crash.

    `effect`, `position`, `size_class`, `text_anchor` are defensively coerced to the
    renderer-known vocab; unknown values fall back to a safe default.

    The trailing kwargs (`font_family`, `stroke_width`, `shadow_enabled`, `text_size_px`,
    `position_x_frac`, `position_y_frac`) carry curated style-set fields resolved by
    the caller (`_inject_agent_intro` → `resolve_overlay_style`). They are honored by
    BOTH renderers (the #296 parity invariant), so an intro now picks up the set's
    typography. When `text_size_px` is given it wins over the `size_class` bucket.

    `behind_subject` (text-behind-subject occlusion) is only ever set on the
    returned dict when True — flag-off / not-requested output must stay
    byte-identical to today's burn dict (the Skia renderer keys its occlusion
    path off the KEY'S PRESENCE, not its truthiness).
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
    if behind_subject:
        overlay["behind_subject"] = True

    # Style-set passthrough. font_family is the headline win (the intro previously
    # rendered with no explicit font); text_size_px overrides the size bucket.
    if font_family:
        overlay["font_family"] = font_family
    if stroke_width is not None:
        overlay["stroke_width"] = int(stroke_width)
    if shadow_enabled is not None:
        overlay["shadow_enabled"] = bool(shadow_enabled)
    if text_size_px is not None:
        overlay["text_size_px"] = int(text_size_px)
        overlay.pop("text_size", None)  # px is authoritative — drop the bucket
    if position_x_frac is not None:
        overlay["position_x_frac"] = float(position_x_frac)
    if position_y_frac is not None:
        overlay["position_y_frac"] = float(position_y_frac)
    if rotation_deg is not None:
        overlay["rotation_deg"] = float(rotation_deg)

    # Parity-gated spacing fields (T11): clamped here so the burn dict always
    # carries renderer-safe values (same clamps as the schema + the TS layout).
    if letter_spacing is not None:
        overlay["letter_spacing"] = resolve_letter_spacing_em(letter_spacing)
    if line_spacing is not None:
        overlay["line_spacing"] = resolve_line_spacing(line_spacing)
    if max_width_frac is not None:
        overlay["max_width_frac"] = resolve_max_width_frac(max_width_frac)

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


# Display end for the persistent hold overlay. Generative output is sub-60s; any
# value past the rendered length works because FFmpeg's `enable='between(t,start,end)'`
# clips the overlay at the real EOF. A generous constant avoids depending on the
# post-beat-snap video duration (unknown at injection time) — under-shooting would
# drop the tail, over-shooting is free.
_HOLD_TO_END_S = 3600.0

# Optional cutoff for the hook text. When passed explicitly to
# build_persistent_intro_overlays / inject_persistent_intro the static hold ends at
# min(hook_window_s, _HOLD_TO_END_S); the default is _HOLD_TO_END_S (text persists for
# the full video — matching the browser preview). Callers that want a timed hook pass
# hook_window_s=HOOK_WINDOW_S (or any desired cutoff).
HOOK_WINDOW_S: float = 3.0


def _record_intro_layout_selected(
    *,
    requested_layout: str,
    layout_source: str | None = None,
    selected_layout: str,
    reason: str,
    text: str,
    word_roles: list[str] | None,
    fallback: bool,
) -> None:
    try:
        from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

        record_pipeline_event(
            "overlay",
            "intro_layout_selected",
            {
                "requested_layout": requested_layout,
                "layout_source": layout_source or "model",
                "selected_layout": selected_layout,
                "reason": reason,
                "text": text,
                "word_count": len((text or "").split()),
                "has_word_roles": bool(word_roles),
                "fallback": fallback,
            },
        )
    except Exception as exc:  # noqa: BLE001 - instrumentation must never break render
        log.warning("intro_layout_selected_event_emit_failed", error=str(exc))


def _build_cluster_intro_overlays(
    *,
    text: str,
    reveal_window_s: float,
    word_roles: list[str] | None,
    text_color: str,
    highlight_color: str,
    cluster_style: dict | None = None,
    language: str = "en",
    hook_window_s: float = _HOLD_TO_END_S,
    behind_subject: bool = False,
    **style_kwargs,
) -> list[dict] | None:
    """Build the editorial word-cluster intro: one [reveal, hold] pair PER block.

    Geometry comes from the deterministic engine in `intro_cluster` (the agent only
    annotates word roles — see that module's docstring). Each block is an ordinary
    overlay using only fields both renderers already honor, so the cluster needs no
    renderer change: reveal is a bounded `fade-in` (well under the animated frame
    cap), hold is one looped static PNG to EOF — the same persistence mechanics as
    the linear intro, multiplied per block.

    `cluster_style` is forwarded verbatim to `compute_cluster_blocks(style=...)`:
    None (default) is the kill-switch contract — byte-identical legacy cluster —
    while `EDITORIAL_STYLE` selects the editorial cascade (the static fallback of
    the transcript-synced sequence, decision D6/D13).

    Returns None when the engine declines the text (word count, fit) or skia is
    unavailable — the caller falls back to the linear intro. Never raises.
    """
    from app.pipeline.intro_cluster import compute_cluster_blocks  # noqa: PLC0415

    base_px = style_kwargs.get("text_size_px")
    blocks = compute_cluster_blocks(
        text,
        word_roles=word_roles,
        base_size_px=int(base_px) if base_px else 60,
        font_family=style_kwargs.get("font_family"),
        reveal_window_s=reveal_window_s,
        style=cluster_style,
        language=language,
    )
    if not blocks:
        return None

    # Per-block style: the cluster owns geometry/size/font/anchor; only the
    # caller's color + stroke survive from the resolved style params.
    stroke_width = style_kwargs.get("stroke_width")
    overlays: list[dict] = []
    for block in blocks:
        common = {
            "position": "center",  # overridden by the explicit fracs below
            "text_anchor": "center",
            "text_color": block.get("text_color") or text_color,
            "highlight_color": highlight_color,
            "font_family": block["font_family"],
            "stroke_width": stroke_width,
            "shadow_enabled": style_kwargs.get("shadow_enabled"),
            "text_size_px": block["text_size_px"],
            "position_x_frac": block["position_x_frac"],
            "position_y_frac": block["position_y_frac"],
            "behind_subject": behind_subject,
        }
        reveal_end = min(block["start_offset_s"] + block["reveal_s"], _HOLD_TO_END_S)
        reveal = build_intro_overlay(
            block["text"],
            effect="fade-in",
            start_s=block["start_offset_s"],
            end_s=reveal_end,
            **common,
        )
        if reveal is not None:
            for key in ("glow_color", "glow_strength"):
                if key in block:
                    reveal[key] = block[key]
            overlays.append(reveal)
        hold = build_intro_overlay(
            block["text"],
            effect="static",
            start_s=reveal_end,
            end_s=min(hook_window_s, _HOLD_TO_END_S),
            **common,
        )
        if hold is not None:
            for key in ("glow_color", "glow_strength"):
                if key in block:
                    hold[key] = block[key]
            overlays.append(hold)
    return overlays or None


def build_persistent_intro_overlays(
    *,
    text: str,
    effect: str,
    reveal_window_s: float,
    beats: list[float] | None = None,
    text_color: str = _DEFAULT_TEXT_COLOR,
    highlight_color: str = _DEFAULT_HIGHLIGHT_COLOR,
    layout: str = "linear",
    requested_layout: str | None = None,
    layout_source: str | None = None,
    layout_reason: str | None = None,
    word_roles: list[str] | None = None,
    cluster_style: dict | None = None,
    language: str = "en",
    hook_window_s: float = _HOLD_TO_END_S,
    start_s: float | None = None,
    end_s: float | None = None,
    behind_subject: bool = False,
    **style_kwargs,
) -> list[dict]:
    """Build the persistent hero-intro as a [reveal, hold] overlay list (absolute,
    section-relative timestamps from 0). Returns [] when nothing is renderable.

    `layout="cluster"` renders the editorial word-cluster (multiple positioned
    blocks, staggered reveal — see `_build_cluster_intro_overlays`); any cluster
    failure falls back to the linear path below, so the cluster can never lose an
    intro that would have rendered. `layout="linear"` (default) is byte-identical
    to the pre-cluster behavior. `cluster_style` (None = legacy, the kill-switch
    contract) selects the cluster engine's style profile — see
    `_build_cluster_intro_overlays`; it never affects the linear path.

    By default the intro text persists for the WHOLE video (matching the browser
    preview): animate in over the opening, then hold statically until EOF. Pass
    `hook_window_s=HOOK_WINDOW_S` (3s) or any other cutoff to time-box the hold.

    The Skia renderer caps ANIMATED overlays at `MAX_OVERLAY_FRAMES` (~4s), so a single
    animated overlay stretched across a longer video would vanish at the cap. Hence two
    overlays:

    - A bounded animated **reveal** `[0, reveal_window_s]` (the agent's effect), short
      enough to stay under the frame cap.
    - A **static hold** `[reveal_window_s, EOF]` in the effect's *settled* color
      (karaoke settles every word to `highlight_color`; other effects settle to
      `text_color`). A static overlay is one looped PNG — no frame cap — and its `end_s`
      spans the entire joined video.

    Both carry `role="generative_intro"`, which the Dedup-1 pass in
    `_collect_absolute_overlays` treats as no-merge (without that the static hold would
    merge into the animated reveal and re-extend it past the frame cap).

    Timestamps run from 0, so for the hero slot (which begins at video t=0) they are
    already absolute — the recipe path (`inject_persistent_intro`) lets
    `_collect_absolute_overlays` rebase them, while the talking-head path burns them
    directly onto the composite via `burn_text_overlays_skia` (also absolute).
    """
    requested = requested_layout or layout
    if not (text or "").strip():
        _record_intro_layout_selected(
            requested_layout=requested,
            layout_source=layout_source,
            selected_layout="linear",
            reason="empty_text",
            text=text,
            word_roles=word_roles,
            fallback=requested == "cluster",
        )
        return []
    effective_start_s = start_s if start_s is not None else 0.0
    effective_hook_s = end_s if end_s is not None else hook_window_s
    reveal_end = effective_start_s + max(0.0, float(reveal_window_s))

    if layout == "cluster":
        cluster_error = False
        try:
            cluster = _build_cluster_intro_overlays(
                text=text,
                reveal_window_s=reveal_end,
                word_roles=word_roles,
                text_color=text_color,
                highlight_color=highlight_color,
                cluster_style=cluster_style,
                language=language,
                hook_window_s=effective_hook_s,
                behind_subject=behind_subject,
                **style_kwargs,
            )
        except Exception as exc:
            log.warning("generative_cluster_intro_failed_falling_back_linear", error=str(exc))
            cluster = None
            cluster_error = True
        if cluster:
            _record_intro_layout_selected(
                requested_layout=requested,
                layout_source=layout_source,
                selected_layout="cluster",
                reason=layout_reason or "agent_pick",
                text=text,
                word_roles=word_roles,
                fallback=False,
            )
            return cluster
        # Engine declined (word count / fit / no skia) → proven linear intro.
        _record_intro_layout_selected(
            requested_layout=requested,
            layout_source=layout_source,
            selected_layout="linear",
            reason="cluster_error_fallback" if cluster_error else "cluster_declined",
            text=text,
            word_roles=word_roles,
            fallback=True,
        )
    else:
        _record_intro_layout_selected(
            requested_layout=requested,
            layout_source=layout_source,
            selected_layout="linear",
            reason=layout_reason or "explicit_linear",
            text=text,
            word_roles=word_roles,
            fallback=requested == "cluster",
        )

    overlays: list[dict] = []
    reveal = build_intro_overlay(
        text,
        effect=effect,
        start_s=effective_start_s,
        end_s=reveal_end,
        beats=beats,
        text_color=text_color,
        highlight_color=highlight_color,
        behind_subject=behind_subject,
        **style_kwargs,
    )
    if reveal is not None:
        overlays.append(reveal)

    # Settled color = what the animated reveal looks like once it finishes. Karaoke
    # sweeps every word to the highlight color; all other effects settle on text_color.
    settled_color = highlight_color if effect == "karaoke-line" else text_color
    hold = build_intro_overlay(
        text,
        effect="static",
        start_s=reveal_end,
        end_s=min(effective_hook_s, _HOLD_TO_END_S),
        text_color=settled_color,
        highlight_color=highlight_color,
        behind_subject=behind_subject,
        **style_kwargs,
    )
    if hold is not None:
        overlays.append(hold)
    return overlays


def inject_persistent_intro(
    recipe: dict,
    hero_slot_index: int,
    *,
    text: str,
    effect: str,
    reveal_window_s: float,
    beats: list[float] | None = None,
    text_color: str = _DEFAULT_TEXT_COLOR,
    highlight_color: str = _DEFAULT_HIGHLIGHT_COLOR,
    layout: str = "linear",
    requested_layout: str | None = None,
    layout_source: str | None = None,
    layout_reason: str | None = None,
    word_roles: list[str] | None = None,
    cluster_style: dict | None = None,
    language: str = "en",
    hook_window_s: float = _HOLD_TO_END_S,
    **style_kwargs,
) -> dict:
    """Inject the persistent hero intro (reveal + static hold) into the hero slot.

    Thin wrapper over `build_persistent_intro_overlays`: builds the [reveal, hold]
    overlay list and appends each into `slots[hero_slot_index]["text_overlays"]`.

    No-op (recipe unchanged) on empty text, missing/empty slots, or an out-of-range
    hero index — same defensive contract as `inject_intro_overlay`.
    """
    if not (text or "").strip():
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

    overlays = build_persistent_intro_overlays(
        text=text,
        effect=effect,
        reveal_window_s=reveal_window_s,
        beats=beats,
        text_color=text_color,
        highlight_color=highlight_color,
        layout=layout,
        requested_layout=requested_layout,
        layout_source=layout_source,
        layout_reason=layout_reason,
        word_roles=word_roles,
        cluster_style=cluster_style,
        language=language,
        hook_window_s=hook_window_s,
        **style_kwargs,
    )
    for overlay in overlays:
        inject_intro_overlay(recipe, hero_slot_index, overlay)
    return recipe


# ── Transcript-synced typographic sequence (the "Editorial · synced" mode) ──────

# Reference-verified emission (frame-by-frame analysis of the target edit):
# blocks POP instantly one at a time on an even beat, the completed phrase
# holds, then the scene HARD-CUTS away — never a crossfade into the successor.
# Only a scene flagged `fade_out` (hold-cap / final scene, see
# `phrase_sequence.split_phrases`) gets an alpha tail.
SEQUENCE_FADE_OUT_MS = 350

# Beat spread: block i reveals at `i * beat` into the scene, where the beat
# divides the scene's life minus a hold tail (the completed phrase must sit
# fully assembled ~0.6s before the cut) and is clamped to the reference's
# observed 0.35–0.8s reveal cadence.
_SEQUENCE_HOLD_TAIL_S = 0.6
_SEQUENCE_BEAT_MIN_S = 0.35
_SEQUENCE_BEAT_MAX_S = 0.8

# Minimum on-screen life per block: a late block whose beat offset would leave
# it visible for less gets its offset pulled in to `scene_len - 0.45`.
_SEQUENCE_MIN_VISIBLE_S = 0.45

# Oldest-first dismantle (reference t=7.0: 'luck' drops 0.2s before 'is just
# a'): on fade_out scenes with >= 3 blocks the FIRST block ends 0.2s early.
_SEQUENCE_DISMANTLE_LEAD_S = 0.2


def _record_sequence_event(event: str, payload: dict) -> None:
    try:
        from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

        record_pipeline_event("overlay", event, payload)
    except Exception as exc:  # noqa: BLE001 - instrumentation must never break render
        log.warning("sequence_event_emit_failed", event=event, error=str(exc))


def build_sequence_overlays(
    scenes: list[dict],
    *,
    base_size_px: int,
    text_color: str = _DEFAULT_TEXT_COLOR,
) -> list[dict] | None:
    """Turn phrase scenes (`phrase_sequence.split_phrases`, `word_roles` already
    filled by the caller) into renderable sequence overlays.

    Per scene: the editorial cluster engine (`compute_cluster_blocks` with
    `style=EDITORIAL_STYLE`) owns the geometry; each block becomes ONE static
    overlay carrying `role="generative_sequence"` — an instant pop-in at its
    beat-spread offset that holds until the scene's end (the reference edit
    shows no per-block fade-in at 0.2s frame sampling). A static overlay
    starting mid-video without `fade_out_ms` stays on the renderer's cheap
    single-PNG loop path. All timestamps are ABSOLUTE from t=0 (the hero-slot
    convention — the talking-head/burn paths consume them directly).

    Timing: the engine's `start_offset_s`/`reveal_s` are IGNORED — block i pops
    at `i * beat` with the beat spread evenly across the scene's life minus the
    hold tail (clamped to the reference cadence; see the constants above), and
    every offset is clamped so the block stays visible >= 0.45s. Scenes end as
    HARD CUTS at `end_s`; only a `fade_out` scene (hold-cap / final) carries
    `fade_out_ms=350`, and when it has >= 3 blocks its first block ends 0.2s
    early (the oldest-word-first dismantle from the reference).

    The whole scene is shifted vertically by `scene_center_y(i) - _CLUSTER_CENTER_Y`
    so consecutive scenes don't sit at the identical y; the styled engine tightens
    its band by `scene_shift_margin`, so the shift preserves the no-clip guarantee.
    `accent_parity=i` alternates the engine's accent assignment per scene.

    A scene the engine declines (fit, glyphs) is skipped — logged + traced — and
    the rest of the sequence still renders. Returns None when NO scene produced an
    overlay, so the caller can fall back to the static styled cluster.
    """
    from app.pipeline.intro_cluster import (  # noqa: PLC0415
        _CLUSTER_CENTER_Y,
        EDITORIAL_STYLE,
        compute_cluster_blocks,
        scene_center_y,
    )

    try:
        from app.pipeline.text_overlay_skia import SEQUENCE_OVERLAY_ROLE  # noqa: PLC0415

        sequence_role = SEQUENCE_OVERLAY_ROLE
    except Exception:  # pragma: no cover — keep this module importable without skia
        sequence_role = "generative_sequence"

    overlays: list[dict] = []
    for i, scene in enumerate(scenes or []):
        words = [str(w) for w in (scene.get("words") or [])]
        text = " ".join(words).strip()
        roles = scene.get("word_roles") or None
        start_s = float(scene.get("start_s") or 0.0)
        end_s = float(scene.get("end_s") or 0.0)
        scene_len = end_s - start_s

        blocks = compute_cluster_blocks(
            text,
            word_roles=list(roles) if roles else None,
            base_size_px=int(base_size_px),
            reveal_window_s=scene_len,
            style=EDITORIAL_STYLE,
            accent_parity=i,
        )
        if not blocks:
            log.info(
                "generative_sequence_scene_skipped",
                scene_index=i,
                text=text,
                word_count=len(words),
            )
            _record_sequence_event(
                "sequence_scene_skipped",
                {"scene_index": i, "text": text, "word_count": len(words)},
            )
            continue

        # Beat-spread reveals: even cadence across the scene's life minus the
        # assembled-phrase hold tail, clamped to the reference's 0.35–0.8s.
        beat = (scene_len - _SEQUENCE_HOLD_TAIL_S) / max(len(blocks) - 1, 1)
        beat = min(_SEQUENCE_BEAT_MAX_S, max(_SEQUENCE_BEAT_MIN_S, beat))
        max_offset = max(scene_len - _SEQUENCE_MIN_VISIBLE_S, 0.0)

        y_shift = scene_center_y(i) - _CLUSTER_CENTER_Y
        fade_out_scene = bool(scene.get("fade_out"))
        for j, block in enumerate(blocks):
            common = {
                "position": "center",  # overridden by the explicit fracs below
                "text_anchor": "center",
                "text_color": block.get("text_color") or text_color,
                "font_family": block["font_family"],
                "text_size_px": block["text_size_px"],
                "position_x_frac": block["position_x_frac"],
                "position_y_frac": block["position_y_frac"] + y_shift,
            }
            block_start = start_s + min(j * beat, max_offset)
            block_end = end_s
            if fade_out_scene and len(blocks) >= 3 and j == 0:
                # Oldest-first dismantle: the first block drops slightly before
                # the rest of the (fading) scene — only when it stays renderable.
                early_end = end_s - _SEQUENCE_DISMANTLE_LEAD_S
                if early_end > block_start:
                    block_end = early_end
            overlay = build_intro_overlay(
                block["text"],
                effect="static",
                start_s=block_start,
                end_s=block_end,
                **common,
            )
            if overlay is None:
                continue
            overlay["role"] = sequence_role
            for key in ("glow_color", "glow_strength"):
                if key in block:
                    overlay[key] = block[key]
            if fade_out_scene:
                overlay["fade_out_ms"] = SEQUENCE_FADE_OUT_MS
            overlays.append(overlay)

    if not overlays:
        log.info("generative_sequence_no_renderable_scenes", scene_count=len(scenes or []))
        return None
    return overlays


# ── TextElement compiler ──────────────────────────────────────────────────────


def _attach_masonry_layer_origin(overlay: dict, element: TextElement) -> None:
    """Forward the board-local text layer origin without changing public fields."""
    params = element.source_params
    if not isinstance(params, dict):
        return
    motion = params.get("masonry_motion")
    if not isinstance(motion, dict):
        return
    origin = motion.get("layer_origin_px")
    if isinstance(origin, bool) or not isinstance(origin, (int, float)):
        return
    if math.isfinite(float(origin)) and float(origin) > 0:
        overlay["masonry_layer_origin_x_px"] = float(origin)


def build_overlays_from_text_elements(
    elements: list[TextElement],
    *,
    video_duration_s: float,
) -> list[dict]:
    """Compile a list of TextElement objects to burn-dict format.

    Byte-identical to the legacy generators for back-compat read-adapter output.
    Reuses existing roles (generative_intro, generative_sequence) and forwards private
    masonry layer-origin metadata for the board-motion compositor.

    Position mapping:  TextElement → burn-dict
      "top"    → "top"
      "bottom" → "bottom"
      "middle" → "center"
      "custom" → "center" + position_x_frac / position_y_frac

    The read adapter converts each legacy burn dict into a *separate* TextElement,
    so adapter-sourced elements never carry ``reveal_s`` — the reveal and hold arrive
    as two independent elements already.  When ``reveal_s`` IS set (directly authored
    elements), the compiler splits one element into a [reveal, hold] pair exactly as
    ``build_persistent_intro_overlays`` does.

    For ``karaoke-line`` elements with stored ``word_timings``, those timings are used
    verbatim (bypassing ``synthesize_word_timings``) to reproduce the original
    beat-snapped values without needing the song's beat list.

    ``text_case`` is resolved HERE (compile time): the burn dict carries the
    transformed text, so the Skia renderer, the Pillow fallback, and the CSS
    preview (which applies the same transform in ``resolveTextElementsLayout``)
    all agree by construction. The stored element keeps the user's casing.
    """
    from app.agents._schemas.text_element import apply_text_case  # noqa: PLC0415

    overlays: list[dict] = []
    for elem in elements:
        if getattr(elem, "removed", False):
            continue
        if getattr(elem, "role", None) == "lyric_line":
            continue
        # ── position → burn-dict position + optional explicit fracs ──────────
        if elem.position == "custom":
            pos = _DEFAULT_POSITION
            pos_x_frac: float | None = elem.x_frac
            pos_y_frac: float | None = elem.y_frac
        elif elem.position == "top":
            pos = "top"
            pos_x_frac = None
            pos_y_frac = None
        elif elem.position == "bottom":
            pos = "bottom"
            pos_x_frac = None
            pos_y_frac = None
        else:  # "middle" → "center"
            pos = "center"
            pos_x_frac = None
            pos_y_frac = None

        effect = elem.effect or _DEFAULT_EFFECT
        text_color = elem.color or _DEFAULT_TEXT_COLOR
        highlight_color_v = elem.highlight_color or _DEFAULT_HIGHLIGHT_COLOR
        text_anchor = elem.alignment or _DEFAULT_ANCHOR
        size_class = elem.size_class or _DEFAULT_SIZE_CLASS
        text_size_px = int(elem.size_px) if elem.size_px is not None else None
        stroke_w = int(elem.stroke_width) if elem.stroke_width is not None else None
        # getattr: Lane E's TextElement.behind_subject field may not be on disk
        # yet (concurrent lane) — absent field reads as False, byte-identical.
        elem_behind_subject = bool(getattr(elem, "behind_subject", False))

        # text_case: transform the display text AND any stored karaoke word
        # timings (their `text` keys are what _draw_karaoke_line burns).
        elem_text = apply_text_case(elem.text, elem.text_case)
        elem_word_timings = elem.word_timings
        if elem.text_case and elem.text_case != "none" and elem_word_timings:
            elem_word_timings = [
                {**wt, "text": apply_text_case(str(wt.get("text", "")), elem.text_case)}
                for wt in elem_word_timings
            ]

        # ── reveal_s: produce [reveal, hold] pair (directly authored only) ───
        # Adapter-sourced elements never set reveal_s (legacy burn dicts don't
        # carry it); those come back as two pre-split TextElements already.
        if elem.reveal_s is not None and effect != "static":
            reveal_end = min(max(0.0, float(elem.reveal_s)), elem.end_s)

            reveal = build_intro_overlay(
                elem_text,
                effect=effect,
                position=pos,
                size_class=size_class,
                text_color=text_color,
                highlight_color=highlight_color_v,
                text_anchor=text_anchor,
                start_s=elem.start_s,
                end_s=reveal_end,
                font_family=elem.font_family,
                stroke_width=stroke_w,
                shadow_enabled=elem.shadow_enabled,
                text_size_px=text_size_px,
                position_x_frac=pos_x_frac,
                position_y_frac=pos_y_frac,
                rotation_deg=elem.rotation_deg,
                letter_spacing=elem.letter_spacing,
                line_spacing=elem.line_spacing,
                max_width_frac=elem.max_width_frac,
                behind_subject=elem_behind_subject,
            )
            if reveal is not None:
                reveal["role"] = elem.role
                if effect == "karaoke-line" and elem_word_timings:
                    reveal["word_timings"] = elem_word_timings
                if elem.fade_out_ms is not None:
                    reveal["fade_out_ms"] = elem.fade_out_ms
                if elem.z is not None:
                    reveal["z"] = elem.z
                _attach_masonry_layer_origin(reveal, elem)
                overlays.append(reveal)

            # Settled color matches the animated reveal's final frame:
            # karaoke sweeps every word to highlight_color; others stay text_color.
            settled = highlight_color_v if effect == "karaoke-line" else text_color
            hold = build_intro_overlay(
                elem_text,
                effect="static",
                position=pos,
                size_class=size_class,
                text_color=settled,
                highlight_color=highlight_color_v,
                text_anchor=text_anchor,
                start_s=reveal_end,
                end_s=elem.end_s,
                font_family=elem.font_family,
                stroke_width=stroke_w,
                shadow_enabled=elem.shadow_enabled,
                text_size_px=text_size_px,
                position_x_frac=pos_x_frac,
                position_y_frac=pos_y_frac,
                rotation_deg=elem.rotation_deg,
                letter_spacing=elem.letter_spacing,
                line_spacing=elem.line_spacing,
                max_width_frac=elem.max_width_frac,
                behind_subject=elem_behind_subject,
            )
            if hold is not None:
                hold["role"] = elem.role
                if elem.fade_out_ms is not None:
                    hold["fade_out_ms"] = elem.fade_out_ms
                if elem.z is not None:
                    hold["z"] = elem.z
                _attach_masonry_layer_origin(hold, elem)
                overlays.append(hold)
            continue

        # ── single-dict path (adapter elements, sequence blocks, static) ─────
        overlay = build_intro_overlay(
            elem_text,
            effect=effect,
            position=pos,
            size_class=size_class,
            text_color=text_color,
            highlight_color=highlight_color_v,
            text_anchor=text_anchor,
            start_s=elem.start_s,
            end_s=elem.end_s,
            font_family=elem.font_family,
            stroke_width=stroke_w,
            shadow_enabled=elem.shadow_enabled,
            text_size_px=text_size_px,
            position_x_frac=pos_x_frac,
            position_y_frac=pos_y_frac,
            rotation_deg=elem.rotation_deg,
            letter_spacing=elem.letter_spacing,
            line_spacing=elem.line_spacing,
            max_width_frac=elem.max_width_frac,
            behind_subject=elem_behind_subject,
        )
        if overlay is None:
            continue

        # build_intro_overlay always emits "generative_intro"; override for
        # sequence elements (role="generative_sequence").
        overlay["role"] = elem.role

        # Use stored word_timings verbatim for karaoke-line so beat-snapped
        # timings from the original render are reproduced exactly (A17).
        if effect == "karaoke-line" and elem_word_timings:
            overlay["word_timings"] = elem_word_timings

        if elem.fade_out_ms is not None:
            overlay["fade_out_ms"] = elem.fade_out_ms

        if elem.z is not None:
            overlay["z"] = elem.z

        _attach_masonry_layer_origin(overlay, elem)
        overlays.append(overlay)

    return overlays
