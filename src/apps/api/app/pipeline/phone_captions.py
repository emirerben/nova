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
    return layers


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
            entry["words"] = words
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
