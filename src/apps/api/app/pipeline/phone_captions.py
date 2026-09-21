"""Compile cloud caption cues into native phone text layers (KRI-132).

Shared by the "Talking to camera" (subtitled) phone lane
(`app.pipeline.phone_subtitled_plan.compile_phone_subtitled_plan`) and, in a
follow-up phase, the narrated phone lane -- both burn cloud caption cues the
same way the cloud renderer does
(`app.tasks.generative_build._render_subtitled_variant` for subtitled;
`assemble_narrated` for narrated) and both need those SAME cues expressed as
native `PortableTextLayer`s instead of an ASS file. See
`app.pipeline.phone_montage_plan` / `app.pipeline.phone_guided_plan` for the
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


def compile_caption_layers(
    cues: list[dict],
    *,
    canvas_width: int,
    canvas_height: int,
    style: str = "sentence",
    id_prefix: str = "caption",
    timeline_duration_s: float | None = None,
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
        overlay = _base_overlay(cue)
        word_timings = (
            _relative_word_timings(cue, cue["words"]) if style == "word" and "words" in cue else []
        )
        if word_timings:
            overlay["effect"] = "karaoke-line"
            overlay["highlight_color"] = _CAPTION_HIGHLIGHT_COLOR
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
    `assets`/`asset_manifest` dicts, exactly as `phone_montage_plan.py` and
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


def _base_overlay(cue: dict) -> dict[str, Any]:
    return {
        "text": cue["text"],
        "start_s": cue["start_s"],
        "end_s": cue["end_s"],
        "font_family": CAPTION_FONT_FAMILY,
        "text_size_px": _CAPTION_TEXT_SIZE_PX,
        "text_color": _CAPTION_TEXT_COLOR,
        "outline_px": _CAPTION_OUTLINE_PX,
        "position_x_frac": 0.5,
        "position_y_frac": 1.0 - _CAPTION_BOTTOM_MARGIN_FRAC,
    }


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
