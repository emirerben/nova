"""Compile cloud caption cues into native phone text layers (KRI-132).

Shared by the "Talking to camera" (subtitled) phone lane
(`app.pipeline.phone_subtitled_plan.compile_phone_subtitled_plan`) and, in a
follow-up phase, the narrated phone lane -- both burn cloud caption cues the
same way the cloud renderer does
(`app.tasks.generative_build._render_subtitled_variant` for subtitled;
`assemble_narrated` for narrated) and both need those SAME cues expressed as
native `PortableTextLayer`s instead of an ASS file. See
`app.pipeline.phone_voiceover_montage_plan` / `app.pipeline.phone_guided_plan` for the
sibling phone compilers this module mirrors in structure, style, and
fail-closed error handling.

Cue shape (verified against the cloud producer,
`app.pipeline.captions.build_plain_cues`, captions.py:127-199, and its
callers in `app.tasks.generative_build._render_subtitled_variant`,
~generative_build.py:20958-20988): each cue is
``{"text": str, "start_s": float, "end_s": float}`` with an OPTIONAL
``"words"`` list of per-word timings, ``[{"text": str, "start_s": float,
"end_s": float}, ...]``, attached when the cue-building call passes
``attach_words=True`` (always true on the subtitled/narrated caption paths).
Word times share the SAME (base-clip) time coordinate as the cue's own
``start_s``/``end_s`` -- they are NOT relative to the cue's start. This exact
shape survives `correct_caption_cues` (`app.pipeline.caption_correct`) and
`resplit_cues_into_sentences` (`captions.py`), both of which run before a cue
reaches this compiler.

No media is downloaded or rendered here; only `PortableTextLayer` geometry is
compiled, exactly like `portable_text_layout.compile_text_overlay`, which
this module delegates to for every actual layout/font/shaping decision so
captions render identically to every other phone text layer.

The phone engine draws the Kria watermark over every export (KRI-548), so a
cue whose lines would run under it is re-broken around it -- see
`watermark_keepout_rect` and `_make_room_for_watermark`. This is phone-only:
the cloud burn carries no mark and never calls this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.kria.portable_text import PortableTextLayer
from app.kria.render_assets import RenderAsset
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan

# Caption look, mirrored from the cloud burn (`app.pipeline.narrated_assembler
# .CAPTION_FONT` and `app.pipeline.captions._ass_header_for`'s
# ``fontsize=78``): TikTok Sans at 78px, white fill, a thin black outline.
# Both caption styles ("sentence" and "word") share this size/font -- only the
# effect and (for "word") the per-word highlight differ, exactly like the two
# cloud ASS styles share one `_ass_caption_header` call.
CAPTION_FONT_FAMILY = "TikTok Sans"
_CAPTION_TEXT_SIZE_PX = 78
_CAPTION_OUTLINE_PX = 4
_CAPTION_TEXT_COLOR = "#FFFFFF"
# `captions.py::_ACTIVE_WORD_ASS_COLOR` is ``"&H0016CC84"`` -- ASS BGR order,
# so (B, G, R) = (0x16, 0xCC, 0x84) is this exact lime in "#RRGGBB".
_CAPTION_HIGHLIGHT_COLOR = "#84CC16"

# `captions.py::SUBTITLED_CAPTION_MARGIN_V` (384) is a pixel MarginV defined
# against a fixed 1920px-tall ASS PlayRes canvas. Convert it to a
# canvas-independent bottom-margin fraction so this compiler can target
# whatever portrait canvas height the phone recipe actually carries (the
# phone story canvas is 1080x1920 today, but this keeps the math honest
# rather than hardcoding 1920 a second time).
_CLOUD_CAPTION_CANVAS_HEIGHT_PX = 1920.0
_SUBTITLED_CAPTION_MARGIN_V = 384.0
_CAPTION_BOTTOM_MARGIN_FRAC = _SUBTITLED_CAPTION_MARGIN_V / _CLOUD_CAPTION_CANVAS_HEIGHT_PX

# Mirrors `EditRecipeV2.text_layers`'s own `Field(max_length=500)` (app/kria/
# recipes_v2.py). Not imported directly -- this module must stay usable
# without constructing a recipe -- but kept in lockstep by
# `test_matches_edit_recipe_v2_text_layer_cap` in this module's test suite.
MAX_CAPTION_LAYERS = 500

# A cue this short after clamping carries no meaningful on-screen window;
# mirrors the minimum cue floor `app.pipeline.captions.build_plain_cues` and
# `lyric_injector._MIN_OVERLAY_DURATION_S` both already enforce.
_MIN_CUE_DURATION_S = 0.01

# The subtitled ASS header's hardcoded MarginL/MarginR (`captions.py::
# _ass_caption_header`'s literal ``"...,2,80,80,{margin_v},1"``) on the SAME
# 1080-wide ASS PlayResX the phone story canvas mirrors -- not derived from
# any persisted field, so this is a literal, not a clamp default.
_CAPTION_LEFT_RIGHT_MARGIN_PX = 80.0
_CAPTION_CANVAS_WIDTH_PX = 1080.0

_HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
_EDITOR_CAPTION_HIGHLIGHT_COLOR = "#C5F82A"

# --- Kria watermark keep-out (KRI-548) ----------------------------------------
#
# Every phone export carries the Kria wordmark (`KriaBranding` in
# KriaMediaEngine/Branding.swift): bottom-left for the whole timeline, drawn
# ABOVE text. On the 1080x1920 reference its opaque mark spans x 60-193,
# y 1416-1475 -- exactly where the first line of a default two- or three-line
# caption sits (block centre y=1536), so a wide line ran under it and lost its
# first letters ("İlk durak..." lost the İ). The mark's corner is brand-approved
# (brand/social/README.md), so captions make room, not the mark.
#
# Mirrors `markLeft` / `markWidth` / `markHeight` / `markBottomInset` and the
# engine's single scale, min(w/1080, h/1920) (`KriaBranding.tileTransform`).
# The numbers are the mark's opaque extent in the bundled PNG;
# `test_watermark_keepout_matches_the_bundled_mark` re-measures the asset.
_WATERMARK_REF_WIDTH_PX = 1080.0
_WATERMARK_REF_HEIGHT_PX = 1920.0
_WATERMARK_MARK_LEFT_PX = 60.0
_WATERMARK_MARK_WIDTH_PX = 133.0
_WATERMARK_MARK_HEIGHT_PX = 59.0
_WATERMARK_MARK_BOTTOM_INSET_PX = 445.0
# Clear air kept between caption ink (glyph outline + outline stroke) and the
# mark. It also covers the mark's own soft shadow, which fades out ~10px past
# the opaque edge.
_WATERMARK_GAP_PX = 12.0
# brand/social/README.md: below y=1530 (of 1920) is every platform's caption /
# username block. A caption that grows a line to clear the mark must not reach
# into it when it was clear of it before.
_PLATFORM_CAPTION_ZONE_TOP_PX = 1530.0
# A cue may gain at most this many lines while making room; past that the
# original layout is kept.
_MAX_EXTRA_CAPTION_LINES = 2
# Last resort, in fractions of a line step: lift the block this far when no
# line split clears the mark where it sits (see `_clear_of_watermark`). 1.5
# steps is about one line plus the mark's height -- enough to lift the line
# level with the mark clear above it. Capped there because the phone lane has
# no face boxes and every extra pixel moves text toward the speaker; past the
# cap the original layout is kept.
_LIFT_FALLBACK_STEPS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5)
# Left-aligned captions start right of the mark instead (their left edge is
# the problem, so a narrower line cannot help). This margin past the stroke
# absorbs a first glyph whose ink starts left of its origin.
_LEFT_EDGE_SAFETY_EM = 0.1

Box = tuple[float, float, float, float]  # left, top, right, bottom (canvas px)


@dataclass(frozen=True, slots=True)
class PhoneCaptionLook:
    """Every caption-appearance knob `compile_caption_layers` honours.

    The default (every field at its listed value) is BYTE-IDENTICAL to this
    module's pre-KRI-216 hardcoded look -- `compile_caption_layers(look=None)`
    and `compile_caption_layers(look=PhoneCaptionLook())` must always compile
    the same layers. `caption_look_from_variant` is the only intended
    constructor for a non-default look; fields mirror the cloud burn's own
    persisted variant fields field-for-field (see that function's docstring).
    """

    captions_enabled: bool = True
    font_family: str = CAPTION_FONT_FAMILY
    text_size_px: int = _CAPTION_TEXT_SIZE_PX
    text_color: str = _CAPTION_TEXT_COLOR
    highlight_color: str = _CAPTION_HIGHLIGHT_COLOR
    outline_px: int = _CAPTION_OUTLINE_PX
    position_y_frac: float = 1.0 - _CAPTION_BOTTOM_MARGIN_FRAC
    # None = not set -- karaoke-line is chosen purely from ``style``
    # (see `compile_caption_layers`); True forces karaoke-line even for
    # ``style="sentence"``; False keeps karaoke-line's word-by-word reveal
    # timing but paints the active word in ``text_color`` (no lime tint) --
    # mirrors the cloud's "word display without tint".
    highlight_spoken_word: bool | None = None
    # None = centered (no `text_anchor`/`vertical_anchor` overlay keys at
    # all, exactly like today). "left"/"right" additionally pins
    # `vertical_anchor="center"` so the y position stays a block CENTER
    # instead of `_resolve_vertical_anchor`'s left-anchor default of "top".
    text_anchor: str | None = None
    position_x_frac: float = 0.5
    # These four are only ever set onto the compiled overlay dict when NOT
    # None -- absent (the default) reproduces today's implicit "standard"
    # text_overlay_skia shadow/outline exactly (see `_base_overlay`).
    stroke_color: str | None = None
    shadow_color: str | None = None
    shadow_opacity: float | None = None
    shadow_enabled: bool | None = None


_DEFAULT_CAPTION_LOOK = PhoneCaptionLook()


def _clamp_int(value: object, lo: int, hi: int, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, parsed))


def _clean_hex_or(value: object, default: str) -> str:
    if isinstance(value, str) and _HEX_COLOR_RE.fullmatch(value.strip()):
        return value.strip().upper()
    return default


def _clean_hex_or_none(value: object) -> str | None:
    if isinstance(value, str) and _HEX_COLOR_RE.fullmatch(value.strip()):
        return value.strip().upper()
    return None


def _bool_or_none(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _shadow_opacity_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(1.0, float(value)))


def caption_look_from_variant(variant: dict) -> PhoneCaptionLook:
    """Project a subtitled variant's persisted caption-appearance fields into a
    `PhoneCaptionLook`, mirroring the cloud burn's own field-by-field
    semantics closely enough that a phone Save's compiled captions match what
    the SAME persisted fields would produce on a cloud reburn:

      - ``voiceover_caption_font`` -> ``font_family``, kept as the RAW
        font-registry KEY (`app.pipeline.narrated_assembler.
        is_valid_caption_font`) -- NEVER resolved to its ASS ``ass_name``
        (`resolve_caption_font`), because `_registry_typeface`
        (`text_overlay_skia.py`) looks up registry keys, not ASS names, and
        an ass_name that happens to differ from every registry key would
        silently fall back to the default typeface. Unknown/deprecated/None
        falls back to `CAPTION_FONT_FAMILY`.
      - ``caption_size_px`` -> ``text_size_px``, clamped 36-160 (mirrors
        `app.pipeline.captions.CaptionStyleOverrides.from_value`), default 78.
      - ``caption_text_color`` / ``caption_highlight_color`` -> ``text_color``
        / ``highlight_color``, default ``#FFFFFF`` / ``#84CC16``.
      - ``caption_stroke_width`` -> ``outline_px``, clamped 0-12, default 4.
      - ``caption_margin_v`` -> ``position_y_frac``, resolved exactly like
        `app.tasks.generative_build._resolve_caption_margin_v` (absent/
        invalid/out-of-band -> the legacy `SUBTITLED_CAPTION_MARGIN_V`
        safe-zone margin) then converted with the same
        ``1 - margin_v / 1920`` this module already used for its hardcoded
        default. NOT re-derived from `generative_build` directly --
        `generative_build` imports `phone_subtitled_plan` (which imports this
        module), so importing back would cycle; the small resolve is mirrored
        here against `app.pipeline.captions`'s public constants instead. Kept
        as a block-CENTER y (this module's pre-existing deviation from the
        cloud's ASS bottom-anchored MarginV) rather than switched to a
        bottom anchor, so an untouched variant's position stays pixel-for-
        pixel where it always was.
      - ``caption_editor_style.alignment`` "left"/"right" -> ``text_anchor``
        (+ an explicit ``position_x_frac`` derived from the SAME 80px
        MarginL/MarginR the cloud ASS header hardcodes on its 1080-wide
        canvas); "center"/absent -> ``None`` (centered, byte-identical).
      - ``caption_editor_style.highlight_spoken_word`` -> passed through
        as-is (``None`` when absent/non-bool).
      - ``caption_editor_style.stroke_color`` / ``shadow_color`` /
        ``shadow_opacity``, and ``caption_shadow_enabled`` (top-level,
        overridable by a ``caption_editor_style.shadow_enabled`` bool, same
        last-write-wins order as `app.tasks.generative_build.
        _caption_style_overrides`'s ``appearance.update(editor_style)``) ->
        the matching `PhoneCaptionLook` field, or ``None`` when absent/
        invalid so `_base_overlay` never sets that overlay key at all.
      - ``captions_enabled`` -> ``captions_enabled`` (``False`` only when the
        field is explicitly ``False``, matching
        `_burn_persisted_captions_onto_base`'s own "absent means on" gate).

    An untouched variant (none of the above fields ever set) resolves every
    field to `PhoneCaptionLook()`'s own default, so a phone Save on a variant
    with no caption edits recompiles byte-identical captions.
    """
    from app.pipeline.captions import (
        CAPTION_Y_FRAC_MAX,
        CAPTION_Y_FRAC_MIN,
        SUBTITLED_CAPTION_MARGIN_V,
        y_frac_to_margin_v,
    )
    from app.pipeline.narrated_assembler import is_valid_caption_font

    captions_enabled = variant.get("captions_enabled", True) is not False

    font_key = variant.get("voiceover_caption_font")
    font_family = (
        font_key
        if isinstance(font_key, str) and font_key and is_valid_caption_font(font_key)
        else CAPTION_FONT_FAMILY
    )

    margin_v = SUBTITLED_CAPTION_MARGIN_V
    raw_margin = variant.get("caption_margin_v")
    if raw_margin is not None:
        try:
            candidate = int(raw_margin)
        except (TypeError, ValueError):
            candidate = None
        if candidate is not None:
            min_margin = y_frac_to_margin_v(CAPTION_Y_FRAC_MAX)
            max_margin = y_frac_to_margin_v(CAPTION_Y_FRAC_MIN)
            if min_margin <= candidate <= max_margin:
                margin_v = candidate

    editor_style = variant.get("caption_editor_style")
    editor_style = editor_style if isinstance(editor_style, dict) else {}

    alignment = editor_style.get("alignment")
    text_anchor = alignment if alignment in ("left", "right") else None
    if text_anchor == "left":
        position_x_frac = _CAPTION_LEFT_RIGHT_MARGIN_PX / _CAPTION_CANVAS_WIDTH_PX
    elif text_anchor == "right":
        position_x_frac = 1.0 - _CAPTION_LEFT_RIGHT_MARGIN_PX / _CAPTION_CANVAS_WIDTH_PX
    else:
        position_x_frac = 0.5

    highlight_spoken_word = _bool_or_none(editor_style.get("highlight_spoken_word"))
    # The cloud's editor caption path (`captions._write_editor_caption_cues`,
    # taken once `highlight_spoken_word` is explicitly set) defaults the active
    # word to #C5F82A -- the same fallback the iOS Settings picker shows. Only
    # the untouched legacy word-pop keeps the #84CC16 lime.
    default_highlight = (
        _EDITOR_CAPTION_HIGHLIGHT_COLOR
        if highlight_spoken_word is not None
        else _CAPTION_HIGHLIGHT_COLOR
    )

    shadow_enabled = _bool_or_none(variant.get("caption_shadow_enabled"))
    editor_shadow_enabled = _bool_or_none(editor_style.get("shadow_enabled"))
    if editor_shadow_enabled is not None:
        shadow_enabled = editor_shadow_enabled

    return PhoneCaptionLook(
        captions_enabled=captions_enabled,
        font_family=font_family,
        text_size_px=_clamp_int(variant.get("caption_size_px"), 36, 160, _CAPTION_TEXT_SIZE_PX),
        text_color=_clean_hex_or(variant.get("caption_text_color"), _CAPTION_TEXT_COLOR),
        highlight_color=_clean_hex_or(variant.get("caption_highlight_color"), default_highlight),
        outline_px=_clamp_int(variant.get("caption_stroke_width"), 0, 12, _CAPTION_OUTLINE_PX),
        position_y_frac=1.0 - (margin_v / _CLOUD_CAPTION_CANVAS_HEIGHT_PX),
        highlight_spoken_word=highlight_spoken_word,
        text_anchor=text_anchor,
        position_x_frac=position_x_frac,
        stroke_color=_clean_hex_or_none(editor_style.get("stroke_color")),
        shadow_color=_clean_hex_or_none(editor_style.get("shadow_color")),
        shadow_opacity=_shadow_opacity_or_none(editor_style.get("shadow_opacity")),
        shadow_enabled=shadow_enabled,
    )


def compile_caption_layers(
    cues: list[dict],
    *,
    canvas_width: int,
    canvas_height: int,
    style: str = "sentence",
    id_prefix: str = "caption",
    timeline_duration_s: float | None = None,
    look: PhoneCaptionLook | None = None,
) -> list[PortableTextLayer]:
    """Compile cloud caption cues into native caption `PortableTextLayer`s.

    ``cues`` are the cloud caption-cue dicts exactly as produced by
    `build_plain_cues`/`correct_caption_cues`/`resplit_cues_into_sentences`
    (see this module's docstring for the exact shape) -- callers pass the
    SAME list they would otherwise hand to `generate_ass_from_cues` /
    `generate_word_pop_ass`.

    ``style``:
      - ``"sentence"`` (default) -- the cloud's "plain" bottom-third look:
        one `PortableTextLayer` per cue, ``effect="pop-in"``.
      - ``"word"`` -- the cloud's word-pop look: a cue WITH per-word timings
        (``cue["words"]``) compiles to ``effect="karaoke-line"`` with a lime
        highlight sweeping word-by-word (`KaraokeContent`); a cue WITHOUT
        word timings falls back to the same ``"pop-in"`` sentence layer
        ``style="sentence"`` would have produced, rather than dropping the
        cue or raising.

    ``look`` (optional, `PhoneCaptionLook`, KRI-216) carries every
    caption-appearance override (font/size/color/outline/position/shadow/
    alignment/highlight) -- see `caption_look_from_variant`. ``None``
    (default) is `PhoneCaptionLook()`'s own default look, byte-identical to
    this module's pre-KRI-216 hardcoded appearance. ``look.captions_enabled
    is False`` compiles NO layers at all (an empty list) -- the clip still
    renders, just caption-free, mirroring `_burn_persisted_captions_onto_base`'s
    own on/off gate. ``look.highlight_spoken_word`` additionally widens which
    cues get the karaoke-line (word-by-word) treatment: `True` forces it even
    for ``style="sentence"``; `False` keeps ``style="word"``'s karaoke-line
    reveal timing but paints the active word in ``look.text_color`` instead
    of ``look.highlight_color`` (no lime tint) -- both mirror the cloud's own
    ``highlight_spoken_word`` semantics.

    Cues are sorted by `start_s`, empty-text cues are dropped, and an
    overlapping cue's `end_s` is clamped to the next cue's `start_s` (the
    device text-layer overlap the cloud's monotonic ASS Dialogue lines never
    have to consider, since libass just draws whatever events are active).
    ``timeline_duration_s``, if given, additionally clamps every cue into
    ``[0, timeline_duration_s]``; a cue that clamps to zero (or negative)
    duration is dropped. Raises `UnsupportedPhonePlan` if the compiled layer
    count would exceed `EditRecipeV2.text_layers`'s own 500-layer cap.

    Layer ids are deterministic: ``f"{id_prefix}-{index}"`` over the
    FINAL (sorted, clamped, filtered) cue list -- never the caller's original
    cue index, so a dropped cue never leaves a gap in the id sequence.

    Watermark (KRI-548): every phone export draws the Kria mark bottom-left,
    above text (`watermark_keepout_rect`). A cue whose ink would touch it is
    re-broken so it does not (`_clear_of_watermark`); every other cue compiles
    exactly as it always has.
    """
    resolved_look = look if look is not None else _DEFAULT_CAPTION_LOOK
    if not resolved_look.captions_enabled:
        return []

    from app.pipeline.canvas import Canvas as PipelineCanvas
    from app.pipeline.portable_text_layout import compile_text_overlay

    prepared = _prepare_cues(cues, timeline_duration_s=timeline_duration_s)
    if len(prepared) > MAX_CAPTION_LAYERS:
        raise UnsupportedPhonePlan(
            f"{len(prepared)} caption cues would exceed the phone recipe's "
            f"{MAX_CAPTION_LAYERS}-text-layer cap"
        )
    canvas = PipelineCanvas(width=canvas_width, height=canvas_height)
    layers: list[PortableTextLayer] = []
    overlays: list[dict[str, Any]] = []
    for index, cue in enumerate(prepared):
        overlay = _base_overlay(cue, resolved_look)
        use_karaoke = "words" in cue and (
            style == "word" or resolved_look.highlight_spoken_word is True
        )
        word_timings = _relative_word_timings(cue, cue["words"]) if use_karaoke else []
        if word_timings:
            overlay["effect"] = "karaoke-line"
            overlay["highlight_color"] = (
                resolved_look.text_color
                if resolved_look.highlight_spoken_word is False
                else resolved_look.highlight_color
            )
            overlay["word_timings"] = word_timings
        else:
            overlay["effect"] = "pop-in"
        layer_id = f"{id_prefix}-{index}"
        try:
            layer, _font = compile_text_overlay(overlay, layer_id=layer_id, canvas=canvas)
        except Exception as exc:  # noqa: BLE001 - untrusted transcript/edited cue content
            raise UnsupportedPhonePlan(f"unable to compile caption cue: {exc}") from exc
        layers.append(layer)
        overlays.append(overlay)
    return _make_room_for_watermark(layers, overlays, canvas=canvas, look=resolved_look)


def watermark_keepout_rect(canvas_width: float, canvas_height: float) -> Box:
    """The device watermark's opaque rect plus the clearance gap, in canvas px.

    Same one-scale rule as `KriaBranding.tileTransform`, so a square or
    landscape canvas gets the proportionally placed mark the engine draws.
    """
    scale = min(canvas_width / _WATERMARK_REF_WIDTH_PX, canvas_height / _WATERMARK_REF_HEIGHT_PX)
    bottom = canvas_height - _WATERMARK_MARK_BOTTOM_INSET_PX * scale
    gap = _WATERMARK_GAP_PX * scale
    return (
        _WATERMARK_MARK_LEFT_PX * scale - gap,
        bottom - _WATERMARK_MARK_HEIGHT_PX * scale - gap,
        (_WATERMARK_MARK_LEFT_PX + _WATERMARK_MARK_WIDTH_PX) * scale + gap,
        bottom + gap,
    )


def caption_ink_box(run: Any, typeface: Any) -> Box | None:
    """Canvas-space ink of one compiled run: glyph outlines plus the outline stroke.

    Positioned (legacy) runs are measured glyph by glyph, exactly as the device
    draws them. A shaped run is laid out by the phone itself, so it is bounded
    by its advance width and the font's full ascent/descent instead.
    """
    from app.pipeline import text_overlay_skia as cloud

    font = _caption_font(typeface, run.font_size)
    if run.glyphs:
        rel = _glyph_ink(font, run.glyphs)
    else:
        metrics = font.getMetrics()
        width = cloud._measure_line(font, run.text, run.letter_spacing, shape_text=True)
        rel = (0.0, metrics.fAscent, width, metrics.fDescent)
    if rel is None:
        return None
    bleed = run.stroke_width / 2
    return (
        run.x + rel[0] - bleed,
        run.baseline_y + rel[1] - bleed,
        run.x + rel[2] + bleed,
        run.baseline_y + rel[3] + bleed,
    )


def _caption_font(typeface: Any, size: float) -> Any:
    import skia

    font = skia.Font(typeface, size)
    font.setSubpixel(True)
    return font


def _glyph_ink(font: Any, glyphs: list[Any], dx: float = 0.0) -> Box | None:
    """Union of the glyph outline bounds, relative to the run origin (+``dx``)."""
    bounds = font.getBounds([glyph.glyph_id for glyph in glyphs])
    boxes = [
        (dx + g.x + b.left(), g.y + b.top(), dx + g.x + b.right(), g.y + b.bottom())
        for g, b in zip(glyphs, bounds, strict=True)
        if not b.isEmpty()
    ]
    return _union(boxes)


def _union(boxes: list[Box]) -> Box | None:
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _intersects(a: Box, b: Box) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _layer_touches(layer: PortableTextLayer, typeface: Any, keepout: Box) -> bool:
    return any(
        (ink := caption_ink_box(run, typeface)) is not None and _intersects(ink, keepout)
        for run in layer.runs
    )


def _make_room_for_watermark(
    layers: list[PortableTextLayer],
    overlays: list[dict[str, Any]],
    *,
    canvas: Any,
    look: PhoneCaptionLook,
) -> list[PortableTextLayer]:
    """Re-break the cues whose ink would sit under the device watermark (KRI-548).

    Centred and right-aligned cues keep their anchor and only change where
    their lines break, so the line crossing the mark's band is short enough to
    clear it. A left-aligned caption's left edge IS the problem, so once any of
    its cues touches the mark, every cue in the set starts just right of the
    mark instead -- one shared edge rather than captions that jump sideways
    from cue to cue. Cues that never touch the mark are returned unchanged.
    """
    if not layers:
        return layers
    from app.pipeline import text_overlay_skia as cloud

    keepout = watermark_keepout_rect(canvas.width, canvas.height)
    try:
        typeface = cloud._resolve_typeface_for_overlay(overlays[0]).typeface
        touching = [_layer_touches(layer, typeface, keepout) for layer in layers]
    except Exception:  # noqa: BLE001 - making room is cosmetic; never fail a render on it
        return layers
    if not any(touching):
        return layers
    shift_left_edge = look.text_anchor == "left"
    result = list(layers)
    for index, (layer, overlay) in enumerate(zip(layers, overlays, strict=True)):
        if not (touching[index] or shift_left_edge):
            continue
        left_edge: tuple[float, float] | None = None
        if shift_left_edge:
            x, _y = cloud._resolve_anchor(overlay, canvas)
            right_bound = x + cloud._overlay_max_width_px(overlay, canvas)
            new_x = keepout[2] + look.outline_px + _LEFT_EDGE_SAFETY_EM * look.text_size_px
            left_edge = (new_x, right_bound - new_x)
        try:
            cleared = _clear_of_watermark(
                overlay,
                layer,
                canvas=canvas,
                keepout=keepout,
                typeface=typeface,
                left_edge=left_edge,
            )
        except Exception:  # noqa: BLE001 - keep today's layer on any surprise
            cleared = None
        if cleared is not None:
            result[index] = cleared
    return result


def _clear_of_watermark(
    overlay: dict[str, Any],
    layer: PortableTextLayer,
    *,
    canvas: Any,
    keepout: Box,
    typeface: Any,
    left_edge: tuple[float, float] | None = None,
) -> PortableTextLayer | None:
    """This cue re-broken so no line's ink meets ``keepout``, or None to keep it.

    Picks the fewest lines (starting from today's count, at most
    `_MAX_EXTRA_CAPTION_LINES` more) for which some line split keeps every
    line inside the usual wrap width AND clear of the mark at the position it
    will actually be drawn; among those splits, the most even line widths
    win. The words, timing, size and anchor point stay as they were. Extra lines
    grow the block about its centre like any longer caption -- unless the cue's
    letters sat above the platform caption zone: then no line may reach into
    it, and the block grows upward instead of down.

    Only when no split clears the mark at the natural position (a long word
    stuck on the line level with the mark: large custom sizes, wide fonts) is
    the block lifted, by at most 1.5 lines (`_LIFT_FALLBACK_STEPS`). It never
    moves down: below is the platforms' caption zone.

    ``left_edge`` (left-aligned captions only) moves the left edge to
    ``left_edge[0]`` and narrows the wrap width to ``left_edge[1]`` so the
    right edge stays where it was.

    The re-broken cue is compiled through the normal path and re-measured;
    any surprise (a shrunk font, ink still on the mark) keeps today's layer, as
    does any exception (caught by `_make_room_for_watermark`).
    """
    from app.pipeline import text_overlay_skia as cloud
    from app.pipeline.portable_text_layout import (
        compile_text_overlay,
        legacy_glyphs_resolvable,
        resolve_legacy_glyphs,
    )

    karaoke = layer.effect == "karaoke-line"
    if not layer.runs:
        return None
    size = layer.runs[0].font_size
    if any(run.font_size != size for run in layer.runs):
        return None
    font = _caption_font(typeface, size)
    updates: dict[str, Any] = {}
    if size != cloud._resolve_font_size_px(overlay):
        # A word wider than the frame already made today's layout shrink this
        # cue; keep that size rather than letting the re-wrap shrink it again.
        updates["text_size_px"] = int(size)
    if left_edge is not None:
        updates["position_x_frac"] = left_edge[0] / canvas.width
        updates["max_width_frac"] = left_edge[1] / canvas.width
    candidate = {**overlay, **updates}
    max_width = cloud._overlay_max_width_px(candidate, canvas)
    anchor = cloud._resolve_text_anchor(candidate)
    cx, cy = cloud._resolve_anchor(candidate, canvas)

    # Words, the breaks the text forces, and each candidate line's width and
    # ink relative to (its left edge, its baseline) -- measured the way the
    # compile path below will place it.
    hard_breaks: set[int] = set()
    if karaoke:
        words = [
            text
            for entry in overlay.get("word_timings") or []
            if (text := str(entry.get("text", "")).strip())
        ]
        spacing = cloud._overlay_letter_spacing_px(overlay, size)
        word_widths = [cloud._measure_line(font, word, spacing) for word in words]
        word_inks = [_glyph_ink(font, resolve_legacy_glyphs(font, w, spacing)) for w in words]
        gap = font.measureText(" ") + 2 * spacing

        def measure(start: int, end: int) -> tuple[float, Box | None]:
            boxes, cursor = [], 0.0
            for i in range(start, end):
                ink = word_inks[i]
                if ink is not None:
                    boxes.append((ink[0] + cursor, ink[1], ink[2] + cursor, ink[3]))
                cursor += word_widths[i] + gap
            return cursor - gap, _union(boxes)

    else:
        text = cloud._overlay_text(overlay)
        raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if any(not raw.split() for raw in raw_lines):
            return None  # an authored blank line; keep the creator's layout
        words = []
        for raw in raw_lines:
            if words:
                hard_breaks.add(len(words))
            words.extend(raw.split())
        spacing = cloud.resolve_letter_spacing_em(overlay.get("letter_spacing")) * size
        shaped = bool(overlay.get("shape_text")) or not legacy_glyphs_resolvable(
            _caption_font(typeface, 40), text
        )
        metrics = font.getMetrics()

        def measure(start: int, end: int) -> tuple[float, Box | None]:
            line = " ".join(words[start:end])
            width = cloud._measure_line(font, line, spacing, shape_text=shaped)
            if shaped:
                return width, (0.0, metrics.fAscent, width, metrics.fDescent)
            return width, _glyph_ink(font, resolve_legacy_glyphs(font, line, spacing))

    if not words:
        return None
    measured: dict[tuple[int, int], tuple[float, Box | None]] = {}

    def segment(start: int, end: int) -> tuple[float, Box | None]:
        if (start, end) not in measured:
            measured[(start, end)] = measure(start, end)
        return measured[(start, end)]

    bleed = layer.runs[0].stroke_width / 2
    line_spacing = cloud.resolve_line_spacing(overlay.get("line_spacing"))
    vertical = cloud._resolve_vertical_anchor(candidate)
    scale = min(canvas.width / _WATERMARK_REF_WIDTH_PX, canvas.height / _WATERMARK_REF_HEIGHT_PX)
    zone_top = canvas.height - (_WATERMARK_REF_HEIGHT_PX - _PLATFORM_CAPTION_ZONE_TOP_PX) * scale

    def block_at(count: int) -> tuple[dict[str, Any], float]:
        block = cloud._measure_block(font, ["x"] * count, line_spacing=line_spacing)
        return block, cloud._vertical_block_top(vertical, cy, block["block_h"])

    today_lines = len({run.baseline_y for run in layer.runs})
    today_block, today_top = block_at(today_lines)
    today_bottom = today_top + today_block["block_h"]
    # Judged by ink: a cue whose letters stayed above the platform zone keeps
    # them there (every line is checked below), and its block may not grow
    # lower than the zone line or where it already ended, whichever is lower.
    today_inks = [ink for run in layer.runs if (ink := caption_ink_box(run, typeface))]
    clear_of_zone = bool(today_inks) and max(ink[3] for ink in today_inks) <= zone_top
    lowest_bottom = max(today_bottom, zone_top)
    word_total = sum(segment(i, i + 1)[0] for i in range(len(words)))
    space = font.measureText(" ")

    counts = [
        count
        for count in range(today_lines, today_lines + _MAX_EXTRA_CAPTION_LINES + 1)
        if count <= len(words)
    ]
    # Natural placements first; a lift is only the fallback when no line split
    # clears the mark where the block would naturally sit.
    placements = [(count, 0.0) for count in counts] + [
        (count, step) for step in _LIFT_FALLBACK_STEPS for count in counts
    ]
    for count, step in placements:
        block, top = block_at(count)
        lift = step * block["line_step"]
        bottom = top + block["block_h"]
        if clear_of_zone and bottom - lift > lowest_bottom:
            lift = bottom - lowest_bottom
        baselines = [
            top - lift + block["ascent_offset"] + k * block["line_step"] for k in range(count)
        ]
        target = (word_total + space * (len(words) - count)) / count

        def line_cost(
            start: int,
            end: int,
            line: int,
            baselines: list[float] = baselines,
            target: float = target,
        ) -> float | None:
            width, ink = segment(start, end)
            if width > max_width and not (karaoke and end - start == 1):
                return None
            if ink is not None:
                left = cloud._anchored_left_x(anchor, cx, width)
                box = (
                    left + ink[0] - bleed,
                    baselines[line] + ink[1] - bleed,
                    left + ink[2] + bleed,
                    baselines[line] + ink[3] + bleed,
                )
                if _intersects(box, keepout) or (clear_of_zone and box[3] > zone_top):
                    return None
            # A lone short word on its own line ("yer.") reads as a mistake; a
            # lone long one ("Yeldeğirmeni'nde.") is just a line.
            orphan = 1.0 if end - start == 1 and width < target / 2 else 0.0
            return orphan + ((width - target) / max_width) ** 2

        rows = _best_rows(len(words), count, hard_breaks, line_cost)
        if rows is None:
            continue
        if karaoke:
            updates["karaoke_row_sizes"] = [end - start for start, end in rows]
        else:
            updates["text"] = "\n".join(" ".join(words[start:end]) for start, end in rows)
        if lift:
            updates["position_y_frac"] = (cy - lift) / canvas.height
        cleared, _font = compile_text_overlay(
            {**overlay, **updates}, layer_id=layer.id, canvas=canvas
        )
        if any(run.font_size != size for run in cleared.runs) or _layer_touches(
            cleared, typeface, keepout
        ):
            return None
        return cleared
    if left_edge is not None and not karaoke:
        # A word too long for the room right of the mark (large custom sizes):
        # let the usual wrap shrink it to fit there, the way a word wider
        # than the frame is already handled, rather than keep it on the mark.
        cleared, _font = compile_text_overlay(candidate, layer_id=layer.id, canvas=canvas)
        if not _layer_touches(cleared, typeface, keepout):
            return cleared
    return None


def _best_rows(
    word_count: int,
    line_count: int,
    hard_breaks: set[int],
    line_cost: Any,
) -> list[tuple[int, int]] | None:
    """Cheapest split of ``word_count`` words into exactly ``line_count`` lines.

    ``line_cost(start, end, line)`` prices words ``[start, end)`` on line
    ``line`` (0-based) or returns None when that line is not allowed. A line
    never spans one of ``hard_breaks`` (word indices that must start a line).
    """
    inf = float("inf")
    best = [[inf] * (word_count + 1) for _ in range(line_count + 1)]
    back = [[-1] * (word_count + 1) for _ in range(line_count + 1)]
    best[0][0] = 0.0
    for line in range(line_count):
        for start in range(word_count):
            if best[line][start] == inf:
                continue
            for end in range(start + 1, word_count + 1):
                if any(start < brk < end for brk in hard_breaks):
                    break
                cost = line_cost(start, end, line)
                if cost is None:
                    continue
                total = best[line][start] + cost
                if total < best[line + 1][end]:
                    best[line + 1][end] = total
                    back[line + 1][end] = start
    if best[line_count][word_count] == inf:
        return None
    rows: list[tuple[int, int]] = []
    end = word_count
    for line in range(line_count, 0, -1):
        start = back[line][end]
        rows.append((start, end))
        end = start
    return rows[::-1]


def caption_font_assets(layers: list[PortableTextLayer]) -> dict[str, RenderAsset]:
    """Library font `RenderAsset`s keyed by id for every run across `layers`.

    `compile_caption_layers` returns `PortableTextLayer`s only, matching its
    locked public signature -- but `EditRecipeV2.validate_asset_manifest`
    (app/kria/recipes_v2.py) requires every text-layer run's
    `font_asset_id` to resolve to a library font asset in the recipe's own
    `asset_manifest`. Callers assembling the full recipe (e.g.
    `phone_subtitled_plan.compile_phone_subtitled_plan`) call this right
    after `compile_caption_layers` and merge the result into their
    `assets`/`asset_manifest` dicts, exactly as `phone_voiceover_montage_plan.py` and
    `phone_guided_plan.py` do with the font asset `compile_text_overlay`
    returns directly.

    Reconstructs each font from its asset id alone, relying on this module's
    own (and `portable_text_layout._compile_text_overlay`'s / `
    _compile_karaoke_overlay`'s) established convention that a font asset id
    is always ``"font-" + <bundled font filename>`` -- so no extra state
    needs to round-trip out of `compile_caption_layers`.
    """
    from app.services.render_library import bundled_font_asset

    assets: dict[str, RenderAsset] = {}
    for layer in layers:
        for run in layer.runs:
            if run.font_asset_id in assets:
                continue
            filename = run.font_asset_id.removeprefix("font-")
            assets[run.font_asset_id] = bundled_font_asset(filename, asset_id=run.font_asset_id)
    return assets


def _base_overlay(cue: dict, look: PhoneCaptionLook) -> dict[str, Any]:
    overlay: dict[str, Any] = {
        "text": cue["text"],
        "start_s": cue["start_s"],
        "end_s": cue["end_s"],
        "font_family": look.font_family,
        "text_size_px": look.text_size_px,
        "text_color": look.text_color,
        "outline_px": look.outline_px,
        "position_x_frac": look.position_x_frac,
        "position_y_frac": look.position_y_frac,
    }
    if look.text_anchor is not None:
        # `_resolve_vertical_anchor` (text_overlay_skia.py) defaults a "left"
        # anchor to a TOP vertical anchor -- pin an explicit "center" so
        # `position_y_frac` keeps meaning the same block-center y it means
        # for the default centered layout.
        overlay["text_anchor"] = look.text_anchor
        overlay["vertical_anchor"] = "center"
    if look.stroke_color is not None:
        overlay["stroke_color"] = look.stroke_color
    if look.shadow_color is not None:
        overlay["shadow_color"] = look.shadow_color
    if look.shadow_opacity is not None:
        overlay["shadow_opacity"] = look.shadow_opacity
    if look.shadow_enabled is not None:
        overlay["shadow_enabled"] = look.shadow_enabled
    return overlay


def _prepare_cues(cues: list[dict], *, timeline_duration_s: float | None) -> list[dict]:
    """Sort, drop empty/degenerate cues, and clamp overlap (and, optionally,
    the timeline bound) -- see `compile_caption_layers`'s docstring."""
    cleaned: list[dict] = []
    for cue in cues:
        text = str(cue.get("text", "")).strip()
        if not text:
            continue
        start = max(0.0, _finite(cue.get("start_s"), 0.0))
        end = _finite(cue.get("end_s"), start)
        if end - start < _MIN_CUE_DURATION_S:
            continue
        entry: dict[str, Any] = {"text": text, "start_s": start, "end_s": end}
        words = cue.get("words")
        if isinstance(words, list) and words:
            entry["words"] = _words_spelling(text, words, start_s=start, end_s=end)
        cleaned.append(entry)
    cleaned.sort(key=lambda entry: entry["start_s"])
    for i in range(len(cleaned) - 1):
        next_start = cleaned[i + 1]["start_s"]
        if cleaned[i]["end_s"] > next_start:
            cleaned[i]["end_s"] = next_start
    if timeline_duration_s is not None:
        bound = max(0.0, float(timeline_duration_s))
        for entry in cleaned:
            entry["start_s"] = min(entry["start_s"], bound)
            entry["end_s"] = min(entry["end_s"], bound)
    return [entry for entry in cleaned if entry["end_s"] - entry["start_s"] >= _MIN_CUE_DURATION_S]


def _words_spelling(text: str, words: list[dict], *, start_s: float, end_s: float) -> list[dict]:
    """``words`` when they still spell ``text``, else an even spread of the text.

    A karaoke line draws its runs from the cue's ``words``, not its ``text``,
    so a text edit that kept the old word list (the chat edit path does) would
    burn the pre-edit words (KRI-280; plan 026 R11). Spelling is compared with
    all whitespace removed, so languages written without spaces keep their
    real timings. On a mismatch the text's own tokens are spread across the
    cue the way the iOS preview already does (`NativeEditorRenderCompiler`):
    equal slices, at least 0.05 s apart.
    """
    spelled = "".join(str(word.get("text", "")) for word in words if isinstance(word, dict))
    if "".join(spelled.split()) == "".join(text.split()):
        return words
    tokens = text.split()
    duration = max(_MIN_CUE_DURATION_S, end_s - start_s)
    spread: list[dict] = []
    cursor = 0.0
    for index, token in enumerate(tokens):
        nxt = max((index + 1) * duration / len(tokens), cursor + 0.05)
        spread.append({"text": token, "start_s": start_s + cursor, "end_s": start_s + nxt})
        cursor = nxt
    return spread


def _relative_word_timings(cue: dict, words: list[dict]) -> list[dict]:
    """Convert a cue's absolute (base-clip time) per-word timings into the
    layer-local offsets `KaraokeContent.starts` (and `_compile_karaoke_overlay`'s
    `word_timings` input) expect -- mirrors `lyric_injector.schedule_karaoke_lines`'s
    identical absolute-to-relative conversion for lyric lines."""
    cue_start = cue["start_s"]
    duration = max(_MIN_CUE_DURATION_S, cue["end_s"] - cue_start)
    timings: list[dict] = []
    for word in words:
        text = str(word.get("text", "")).strip()
        if not text:
            continue
        rel_start = max(0.0, _finite(word.get("start_s"), cue_start) - cue_start)
        if rel_start >= duration:
            continue  # the word falls entirely past this (possibly clamped) cue window
        rel_end_abs = _finite(word.get("end_s"), cue_start) - cue_start
        rel_end = min(duration, max(rel_start + 0.01, rel_end_abs))
        timings.append({"text": text, "start_s": round(rel_start, 3), "end_s": round(rel_end, 3)})
    return timings


def _finite(value: object, default: float) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result if result == result and result not in (float("inf"), float("-inf")) else default
