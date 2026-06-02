"""Universal text-overlay constraint pass — the hard "text always fits" guarantee.

Runs on EVERY overlay collected by `_collect_absolute_overlays`
(agentic templates + music lyrics + generative edits — classic templates never
reach this path) after styling is resolved and before the renderer fork. It is a
pure post-process on the canonical overlay dict, so it behaves identically for
all paths and is independent of which style set was chosen.

What it enforces, per overlay:
  • max line count (default 3) and max line width within the 9:16 safe zone —
    by shrinking `text_size_px` (never the text) down to a legibility floor;
    overlays with `preserve_font_size=True` opt out of size shrinkage and wrap
    at their configured size instead;
  • the rendered text block stays inside the safe-zone margins — by clamping
    `position_y_frac` and (anchor-aware) `position_x_frac`.

Renderer-parity (CLAUDE.md #296 class): the pass only ever writes `text_size_px`
and `position_*_frac`, both honored identically by the Pillow and Skia
renderers. It measures via `text_overlay.wrap_and_measure` — the shared helper
whose greedy wrap mirrors both renderers' `_wrap_text_to_lines` — and wraps at a
slightly tighter budget than the safe zone so a fit it approves is safe under
Skia's HarfBuzz shaping too.
"""

from __future__ import annotations

import structlog

from app.pipeline.text_overlay import (
    _FONT_SIZE_MAP,
    _POSITION_Y,
    _TEXT_LINE_SPACING,
    CANVAS_H,
    CANVAS_W,
    wrap_and_measure,
)

log = structlog.get_logger()

# Legibility floor — matches the Skia renderer's _MIN_FONT_SIZE and Pillow's
# shrink-to-fit minimum so the constraint pass never asks for a size a renderer
# would refuse.
_MIN_FONT_SIZE = 24
# Multiplicative shrink step per iteration (≈12% smaller each pass).
_SHRINK_FACTOR = 0.88
_MAX_SHRINK_ITERS = 12
# Extra headroom on the width budget so a Pillow-approved fit survives Skia's
# (slightly wider) HarfBuzz shaping.
_SKIA_SAFETY = 0.97
# Effects whose renderers draw the text on a SINGLE line (no wrap): the karaoke
# sweep and the calm lyric line both lay the whole line out horizontally. Legacy
# cumulative word-reveals also shrink as one growing line unless they explicitly
# request fixed typography via preserve_font_size.
_SINGLE_LINE_EFFECTS = {"karaoke-line", "lyric-line"}
# Horizontal clamp uses a near-zero margin (frame bound), NOT the vertical safe
# margin: the 6% safe zone exists for the TikTok/Reels UI chrome at the top and
# bottom, but content legitimately sits close to the left/right edges (Layer-2
# word-reveal deliberately left-anchors at position_x_frac=0.05). We only pull a
# line in when it would actually run off the frame.
_X_SAFE_MARGIN_FRAC = 0.0


def _current_size_px(overlay: dict) -> int:
    px = overlay.get("text_size_px")
    if px:
        return max(_MIN_FONT_SIZE, int(px))
    return max(_MIN_FONT_SIZE, _FONT_SIZE_MAP.get(overlay.get("text_size", "medium"), 72))


def _block_center_y_frac(overlay: dict) -> float:
    y = overlay.get("position_y_frac")
    if y is not None:
        return float(y)
    return _POSITION_Y.get(overlay.get("position", "center"), 0.5)


def apply_overlay_constraints(
    overlays: list[dict],
    *,
    canvas_w: int = CANVAS_W,
    canvas_h: int = CANVAS_H,
    safe_margin_frac: float = 0.06,
    max_lines: int = 3,
) -> list[dict]:
    """Shrink + reposition every overlay so it fits the 9:16 safe zone.

    Mutates and returns `overlays` (same list, in place) — matches the style of
    the surrounding `_collect_absolute_overlays` dedup passes. Each clamp emits a
    `pipeline_trace` event (`constraint_clamp`, or `constraint_floor_hit` when
    the legibility floor is reached with text still overflowing) so the admin
    job-debug view shows exactly what was auto-fixed.
    """
    safe_w = (1.0 - 2.0 * safe_margin_frac) * canvas_w
    width_budget = int(safe_w * _SKIA_SAFETY)

    for ov in overlays:
        text = ov.get("text") or ""
        if not text.strip():
            continue

        font_family = ov.get("font_family")
        font_style = ov.get("font_style", "display")
        size_px = _current_size_px(ov)
        original_size_px = size_px

        preserve_font_size = bool(ov.get("preserve_font_size"))

        # Single-line effects must fit the WHOLE line horizontally (the renderer
        # never wraps them). Measuring with a very wide budget keeps it one line
        # so the loop shrinks until the full line fits `width_budget`.
        single_line = ov.get("effect") in _SINGLE_LINE_EFFECTS or (
            bool(ov.get("pop_animated_suffix")) and not preserve_font_size
        )
        ov_max_lines = 1 if single_line else max_lines
        measure_budget = canvas_w * 10 if single_line else width_budget

        lines, widest = wrap_and_measure(
            text,
            font_family=font_family,
            font_style=font_style,
            text_size_px=size_px,
            max_width_px=measure_budget,
        )
        # widest == 0 means the font couldn't be resolved — leave the renderer's
        # own shrink-to-fit net to handle it rather than guessing.
        floor_hit = False
        iters = 0
        while (
            not preserve_font_size
            and widest > 0
            and (len(lines) > ov_max_lines or widest > width_budget)
        ):
            if size_px <= _MIN_FONT_SIZE or iters >= _MAX_SHRINK_ITERS:
                if len(lines) > ov_max_lines or widest > width_budget:
                    floor_hit = True
                break
            size_px = max(_MIN_FONT_SIZE, int(size_px * _SHRINK_FACTOR))
            iters += 1
            lines, widest = wrap_and_measure(
                text,
                font_family=font_family,
                font_style=font_style,
                text_size_px=size_px,
                max_width_px=measure_budget,
            )

        size_changed = size_px != original_size_px
        if size_changed:
            ov["text_size_px"] = size_px

        # ── Reposition the block into the safe zone ──────────────────────────
        line_count = max(1, len(lines))
        block_h_frac = (line_count * size_px * _TEXT_LINE_SPACING) / canvas_h
        anchor = ov.get("text_anchor", "center")

        # Vertical. For left-anchored text position_y_frac is the block TOP
        # (mirrors renderer _vertical_block_top); for center/right it is the
        # block CENTER. Clamp whichever so the whole block stays in margins.
        y = _block_center_y_frac(ov)
        y_changed = False
        if anchor == "left":
            top = y
            top = min(
                max(top, safe_margin_frac),
                max(safe_margin_frac, 1.0 - safe_margin_frac - block_h_frac),
            )
            if abs(top - y) > 1e-4:
                ov["position_y_frac"] = round(top, 4)
                y_changed = True
        else:
            half = block_h_frac / 2.0
            lo, hi = safe_margin_frac + half, 1.0 - safe_margin_frac - half
            if lo <= hi:
                clamped = min(max(y, lo), hi)
                if abs(clamped - y) > 1e-4:
                    ov["position_y_frac"] = round(clamped, 4)
                    y_changed = True

        # Horizontal. Only meaningful for non-centered anchors or when an
        # explicit x_frac was set; centered text on x=0.5 with a fit width is
        # already symmetric within the safe zone.
        x_changed = False
        x = ov.get("position_x_frac")
        if x is not None and widest > 0:
            half_w_frac = (widest / canvas_w) / 2.0
            m = _X_SAFE_MARGIN_FRAC
            x = float(x)
            if anchor == "left":
                lo, hi = m, 1.0 - m - (widest / canvas_w)
            elif anchor == "right":
                lo, hi = m + (widest / canvas_w), 1.0 - m
            else:
                lo, hi = m + half_w_frac, 1.0 - m - half_w_frac
            if lo <= hi:
                clamped = min(max(x, lo), hi)
                if abs(clamped - x) > 1e-4:
                    ov["position_x_frac"] = round(clamped, 4)
                    x_changed = True

        if size_changed or y_changed or x_changed or floor_hit:
            _emit(
                ov,
                original_size_px,
                size_px,
                size_changed,
                y_changed,
                x_changed,
                floor_hit,
                line_count,
            )

    return overlays


def _emit(
    ov: dict,
    from_px: int,
    to_px: int,
    size_changed: bool,
    y_changed: bool,
    x_changed: bool,
    floor_hit: bool,
    line_count: int,
) -> None:
    try:
        from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

        record_pipeline_event(
            stage="overlay",
            event="constraint_floor_hit" if floor_hit else "constraint_clamp",
            data={
                "text": (ov.get("text") or "")[:80],
                "font_size_from": from_px,
                "font_size_to": to_px,
                "size_shrunk": size_changed,
                "y_clamped": y_changed,
                "x_clamped": x_changed,
                "line_count": line_count,
                "floor_hit": floor_hit,
            },
        )
    except Exception:  # noqa: BLE001 — trace write must never break a render
        log.warning("constraint_trace_emit_failed", exc_info=True)
