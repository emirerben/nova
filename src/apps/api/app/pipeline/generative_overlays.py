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
    text_size_px: int | None = None,
    position_x_frac: float | None = None,
    position_y_frac: float | None = None,
) -> dict | None:
    """Build a single hero-intro overlay dict in the Skia overlay schema.

    Returns None when there is nothing renderable (empty text or non-positive window)
    so the caller can skip injection and render footage without an intro — never crash.

    `effect`, `position`, `size_class`, `text_anchor` are defensively coerced to the
    renderer-known vocab; unknown values fall back to a safe default.

    The trailing kwargs (`font_family`, `stroke_width`, `text_size_px`,
    `position_x_frac`, `position_y_frac`) carry curated style-set fields resolved by
    the caller (`_inject_agent_intro` → `resolve_overlay_style`). They are honored by
    BOTH renderers (the #296 parity invariant), so an intro now picks up the set's
    typography. When `text_size_px` is given it wins over the `size_class` bucket.
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

    # Style-set passthrough. font_family is the headline win (the intro previously
    # rendered with no explicit font); text_size_px overrides the size bucket.
    if font_family:
        overlay["font_family"] = font_family
    if stroke_width is not None:
        overlay["stroke_width"] = int(stroke_width)
    if text_size_px is not None:
        overlay["text_size_px"] = int(text_size_px)
        overlay.pop("text_size", None)  # px is authoritative — drop the bucket
    if position_x_frac is not None:
        overlay["position_x_frac"] = float(position_x_frac)
    if position_y_frac is not None:
        overlay["position_y_frac"] = float(position_y_frac)

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


def _record_intro_layout_selected(
    *,
    requested_layout: str,
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
    **style_kwargs,
) -> list[dict] | None:
    """Build the editorial word-cluster intro: one [reveal, hold] pair PER block.

    Geometry comes from the deterministic engine in `intro_cluster` (the agent only
    annotates word roles — see that module's docstring). Each block is an ordinary
    overlay using only fields both renderers already honor, so the cluster needs no
    renderer change: reveal is a bounded `fade-in` (well under the animated frame
    cap), hold is one looped static PNG to EOF — the same persistence mechanics as
    the linear intro, multiplied per block.

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
            "text_color": text_color,
            "highlight_color": highlight_color,
            "font_family": block["font_family"],
            "stroke_width": stroke_width,
            "text_size_px": block["text_size_px"],
            "position_x_frac": block["position_x_frac"],
            "position_y_frac": block["position_y_frac"],
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
            overlays.append(reveal)
        hold = build_intro_overlay(
            block["text"],
            effect="static",
            start_s=reveal_end,
            end_s=_HOLD_TO_END_S,
            **common,
        )
        if hold is not None:
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
    layout_reason: str | None = None,
    word_roles: list[str] | None = None,
    **style_kwargs,
) -> list[dict]:
    """Build the persistent hero-intro as a [reveal, hold] overlay list (absolute,
    section-relative timestamps from 0). Returns [] when nothing is renderable.

    `layout="cluster"` renders the editorial word-cluster (multiple positioned
    blocks, staggered reveal — see `_build_cluster_intro_overlays`); any cluster
    failure falls back to the linear path below, so the cluster can never lose an
    intro that would have rendered. `layout="linear"` (default) is byte-identical
    to the pre-cluster behavior.

    A persistent intro keeps the hero text on screen for the WHOLE video: animate it in
    over the opening, then hold the settled text statically until the video ends. The
    Skia renderer caps ANIMATED overlays at `MAX_OVERLAY_FRAMES` (~4s), so a single
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
            selected_layout="linear",
            reason="empty_text",
            text=text,
            word_roles=word_roles,
            fallback=requested == "cluster",
        )
        return []
    reveal_end = max(0.0, float(reveal_window_s))

    if layout == "cluster":
        cluster_error = False
        try:
            cluster = _build_cluster_intro_overlays(
                text=text,
                reveal_window_s=reveal_end,
                word_roles=word_roles,
                text_color=text_color,
                highlight_color=highlight_color,
                **style_kwargs,
            )
        except Exception as exc:
            log.warning("generative_cluster_intro_failed_falling_back_linear", error=str(exc))
            cluster = None
            cluster_error = True
        if cluster:
            _record_intro_layout_selected(
                requested_layout=requested,
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
            selected_layout="linear",
            reason="cluster_error_fallback" if cluster_error else "cluster_declined",
            text=text,
            word_roles=word_roles,
            fallback=True,
        )
    else:
        _record_intro_layout_selected(
            requested_layout=requested,
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
        start_s=0.0,
        end_s=reveal_end,
        beats=beats,
        text_color=text_color,
        highlight_color=highlight_color,
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
        end_s=_HOLD_TO_END_S,
        text_color=settled_color,
        highlight_color=highlight_color,
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
    layout_reason: str | None = None,
    word_roles: list[str] | None = None,
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
        layout_reason=layout_reason,
        word_roles=word_roles,
        **style_kwargs,
    )
    for overlay in overlays:
        inject_intro_overlay(recipe, hero_slot_index, overlay)
    return recipe
