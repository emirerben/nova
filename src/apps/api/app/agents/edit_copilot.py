"""nova.edit.copilot — parse chat edits into full-editor draft operations.

The copilot endpoint is stateless: the client sends the current draft snapshot
on every turn, this agent proposes structured ops, and the server parser drops
anything outside the v1 op vocabulary before returning it. The endpoint never
writes those ops to Job/PlanItem rows; the editor applies them to local draft
state and the existing editor-commit path validates again on Save.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, ClassVar, Literal

import structlog
from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.agents._schemas.text_element import _ALLOWED_EFFECTS, _ALLOWED_FONTS, _HEX_COLOR_RE
from app.agents.music_matcher import _sanitize_text
from app.config import settings
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

EDIT_COPILOT_PROMPT_VERSION = "2026-08-08-v15"
_CONFIDENCE_CLARIFY_THRESHOLD = 0.55
# Coupled surfaces: prompts/edit_copilot.txt prose ("up to 12", twice) and the
# eval structural gate (tests/evals/runners/structural.py imports this).
_MAX_OPS = 12
# Renderer-side guard only — the producer (snapshot.ts COPILOT_BEAT_MARKS_MAX)
# stride-caps to the same count before sending, preserving late-video marks.
_BEAT_MARKS_SHOWN_MAX = 60
# Speech-section guards — the producer (snapshot.ts) head-caps to the same
# counts (head-biased: early words carry the hook window).
_SPEECH_WORDS_SHOWN_MAX = 150
_PAUSE_MARKS_SHOWN_MAX = 40
_SFX_SUGGESTIONS_SHOWN_MAX = 6
_TEXT_INDEX_KEYS = ("text_bars", "textBars", "bars", "text_elements", "textElements")
_SLOT_INDEX_KEYS = ("slots", "local_slots", "localSlots")

_VALID_INTENTS = {"edit", "clarify", "describe", "reject", "unknown"}
_TEXT_OPS = {"edit_text", "set_text_timing", "add_text", "remove_text"}
_STYLE_OPS = {"patch_text_style"}
_CLIP_OPS = {"set_clip_duration", "set_clip_in", "reorder_clip", "remove_clip", "split_clip"}
_SFX_OPS = {"add_sfx", "patch_sfx", "remove_sfx"}
_OVERLAY_OPS = {
    "add_overlay",
    "patch_overlay",
    "remove_overlay",
    "accept_overlay_suggestion",
}
_CAPTION_OPS = {
    "edit_caption",
    "replace_caption_text",
    "set_caption_timing",
    "set_caption_meta",
    "set_caption_emphasis",
}
_MUSIC_OPS = {"swap_music", "set_mix"}
_RENDER_OPS = frozenset({"set_intro_layout", "set_carousel_moment"})
_TITLE_OPS = {"set_title"}
_TOOL_OPS = {"open_tool"}
_EFFECT_OPS = {"add_camera_effect", "patch_camera_effect", "remove_camera_effect"}
_TRANSITION_OPS = {"set_transition"}
_VISUAL_OPS = {"set_visual_fade"}
_MOTION_OPS = {"add_motion_block", "patch_motion_block", "remove_motion_block"}
_VALID_OPS = (
    _TEXT_OPS
    | _STYLE_OPS
    | _CLIP_OPS
    | _SFX_OPS
    | _OVERLAY_OPS
    | _CAPTION_OPS
    | _MUSIC_OPS
    | _RENDER_OPS
    | _TITLE_OPS
    | _TOOL_OPS
    | _EFFECT_OPS
    | _TRANSITION_OPS
    | _VISUAL_OPS
    | _MOTION_OPS
)

_OP_REQUIRED: dict[str, frozenset[str]] = {
    "edit_text": frozenset({"bar_index", "text"}),
    "patch_text_style": frozenset({"bar_index", "patch"}),
    "set_text_timing": frozenset({"bar_index"}),
    "add_text": frozenset({"text", "start_s", "end_s"}),
    "remove_text": frozenset({"bar_index"}),
    "set_clip_duration": frozenset({"slot_index", "duration_s"}),
    "set_clip_in": frozenset({"slot_index", "in_s"}),
    "reorder_clip": frozenset({"from_index", "to_index"}),
    "remove_clip": frozenset({"slot_index"}),
    "split_clip": frozenset({"slot_index", "at_s"}),
    "add_sfx": frozenset({"effect_id", "at_s"}),
    "patch_sfx": frozenset({"sfx_index"}),
    "remove_sfx": frozenset({"sfx_index"}),
    "add_overlay": frozenset({"asset_id", "start_s", "end_s"}),
    "patch_overlay": frozenset({"overlay_index", "patch"}),
    "remove_overlay": frozenset({"overlay_index"}),
    "accept_overlay_suggestion": frozenset({"suggestion_id"}),
    "edit_caption": frozenset({"cue_index", "text"}),
    "replace_caption_text": frozenset({"find", "replace"}),
    "set_caption_timing": frozenset({"cue_index"}),
    "set_caption_meta": frozenset({"patch"}),
    "set_caption_emphasis": frozenset({"cue_index", "emphasis"}),
    "swap_music": frozenset({"track_id"}),
    "set_mix": frozenset({"music_level"}),
    "set_intro_layout": frozenset({"layout"}),
    "set_carousel_moment": frozenset({"config"}),
    "set_title": frozenset({"title"}),
    "open_tool": frozenset({"tool"}),
    "add_camera_effect": frozenset({"start_s", "end_s"}),
    "patch_camera_effect": frozenset({"camera_effect_index"}),
    "remove_camera_effect": frozenset({"camera_effect_index"}),
    "set_transition": frozenset({"boundary_index", "transition"}),
    "set_visual_fade": frozenset({"visual_block_index"}),
    "add_motion_block": frozenset({"preset_id", "start_s", "end_s", "params"}),
    "patch_motion_block": frozenset({"motion_id", "patch"}),
    "remove_motion_block": frozenset({"motion_id"}),
}

_OP_FIELDS: dict[str, frozenset[str]] = {
    "edit_text": frozenset({"bar_index", "text"}),
    "patch_text_style": frozenset({"bar_index", "patch"}),
    "set_text_timing": frozenset({"bar_index", "start_s", "end_s"}),
    "add_text": frozenset({"text", "start_s", "end_s"}),
    "remove_text": frozenset({"bar_index"}),
    "set_clip_duration": frozenset({"slot_index", "duration_s"}),
    "set_clip_in": frozenset({"slot_index", "in_s"}),
    "reorder_clip": frozenset({"from_index", "to_index"}),
    "remove_clip": frozenset({"slot_index"}),
    "split_clip": frozenset({"slot_index", "at_s"}),
    "add_sfx": frozenset({"effect_id", "at_s", "gain"}),
    "patch_sfx": frozenset({"sfx_index", "at_s", "gain"}),
    "remove_sfx": frozenset({"sfx_index"}),
    "add_overlay": frozenset(
        {
            "asset_id",
            "start_s",
            "end_s",
            "position",
            "x_frac",
            "y_frac",
            "scale",
            "display_mode",
        }
    ),
    "patch_overlay": frozenset({"overlay_index", "patch"}),
    "remove_overlay": frozenset({"overlay_index"}),
    "accept_overlay_suggestion": frozenset({"suggestion_id"}),
    "edit_caption": frozenset({"cue_index", "text"}),
    "replace_caption_text": frozenset({"find", "replace"}),
    "set_caption_timing": frozenset({"cue_index", "start_s", "end_s"}),
    "set_caption_meta": frozenset({"patch"}),
    "set_caption_emphasis": frozenset({"cue_index", "emphasis"}),
    "swap_music": frozenset({"track_id"}),
    "set_mix": frozenset({"music_level"}),
    "set_intro_layout": frozenset({"layout"}),
    "set_carousel_moment": frozenset({"config"}),
    "set_title": frozenset({"title"}),
    "open_tool": frozenset({"tool"}),
    "add_camera_effect": frozenset({"start_s", "end_s", "intensity"}),
    "patch_camera_effect": frozenset({"camera_effect_index", "start_s", "end_s", "intensity"}),
    "remove_camera_effect": frozenset({"camera_effect_index"}),
    "set_transition": frozenset({"boundary_index", "transition", "duration_s"}),
    "set_visual_fade": frozenset({"visual_block_index", "transition_in", "transition_out"}),
    "add_motion_block": frozenset(
        {"preset_id", "start_s", "end_s", "params", "palette", "intensity"}
    ),
    "patch_motion_block": frozenset({"motion_id", "patch"}),
    "remove_motion_block": frozenset({"motion_id"}),
}

# Field-exact examples for proactive Director prompts. Keep these next to the
# parser contract so a field rename cannot silently leave the model with only
# operation names. Render and navigation operations are intentionally absent:
# Director cards must apply an undoable local draft change.
_DIRECTOR_OPERATION_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("edit_text", '{"op":"edit_text","bar_index":0,"text":"new hook"}'),
    (
        "patch_text_style",
        '{"op":"patch_text_style","bar_index":0,'
        '"patch":{"size_px":54,"font_family":"Playfair Display"}}',
    ),
    (
        "set_text_timing",
        '{"op":"set_text_timing","bar_index":0,"start_s":0.2,"end_s":2.8}',
    ),
    (
        "add_text",
        '{"op":"add_text","text":"new label","start_s":1.0,"end_s":3.0}',
    ),
    ("remove_text", '{"op":"remove_text","bar_index":0}'),
    (
        "set_clip_duration",
        '{"op":"set_clip_duration","slot_index":1,"duration_s":3.0}',
    ),
    ("set_clip_in", '{"op":"set_clip_in","slot_index":1,"in_s":0.8}'),
    ("reorder_clip", '{"op":"reorder_clip","from_index":2,"to_index":0}'),
    ("remove_clip", '{"op":"remove_clip","slot_index":1}'),
    ("split_clip", '{"op":"split_clip","slot_index":0,"at_s":4.2}'),
    (
        "add_sfx",
        '{"op":"add_sfx","effect_id":"sfx_pop","at_s":1.2,"gain":1.0}',
    ),
    (
        "patch_sfx",
        '{"op":"patch_sfx","sfx_index":0,"at_s":1.4,"gain":0.8}',
    ),
    ("remove_sfx", '{"op":"remove_sfx","sfx_index":0}'),
    (
        "add_overlay",
        '{"op":"add_overlay","asset_id":"asset_1","start_s":1.0,'
        '"end_s":3.5,"position":"bottom","scale":0.5}',
    ),
    (
        "patch_overlay",
        '{"op":"patch_overlay","overlay_index":0,'
        '"patch":{"start_s":1.2,"end_s":4.0,"display_mode":"fullscreen"}}',
    ),
    ("remove_overlay", '{"op":"remove_overlay","overlay_index":0}'),
    (
        "accept_overlay_suggestion",
        '{"op":"accept_overlay_suggestion","suggestion_id":"suggestion_1"}',
    ),
    (
        "edit_caption",
        '{"op":"edit_caption","cue_index":0,"text":"corrected caption text"}',
    ),
    (
        "replace_caption_text",
        '{"op":"replace_caption_text","find":"Kriya","replace":"Kria"}',
    ),
    (
        "set_caption_timing",
        '{"op":"set_caption_timing","cue_index":0,"start_s":0.2,"end_s":1.8}',
    ),
    (
        "set_caption_meta",
        '{"op":"set_caption_meta",'
        '"patch":{"enabled":true,"style":"word","font":"TikTok Sans Bold","y_frac":0.82}}',
    ),
    (
        "set_caption_emphasis",
        '{"op":"set_caption_emphasis","cue_index":0,"emphasis":true}',
    ),
    ("swap_music", '{"op":"swap_music","track_id":"track_1"}'),
    ("set_mix", '{"op":"set_mix","music_level":0.35}'),
    ("set_title", '{"op":"set_title","title":"new working title"}'),
    (
        "add_camera_effect",
        '{"op":"add_camera_effect","start_s":1.0,"end_s":2.2,"intensity":0.04}',
    ),
    (
        "patch_camera_effect",
        '{"op":"patch_camera_effect","camera_effect_index":0,"intensity":0.06}',
    ),
    (
        "remove_camera_effect",
        '{"op":"remove_camera_effect","camera_effect_index":0}',
    ),
    (
        "set_transition",
        '{"op":"set_transition","boundary_index":0,"transition":"crossfade","duration_s":0.3}',
    ),
    (
        "set_visual_fade",
        '{"op":"set_visual_fade","visual_block_index":0,'
        '"transition_in":"fade","transition_out":"cut"}',
    ),
    (
        "add_motion_block",
        '{"op":"add_motion_block","preset_id":"kinetic_word",'
        '"start_s":0.0,"end_s":2.5,"params":{"text":"GO WILD"}}',
    ),
    (
        "patch_motion_block",
        '{"op":"patch_motion_block","motion_id":"motion_1","patch":{"params":{"text":"NEW HOOK"}}}',
    ),
    ("remove_motion_block", '{"op":"remove_motion_block","motion_id":"motion_1"}'),
)

_STYLE_PATCH_FIELDS = frozenset(
    {
        "font_family",
        "size_px",
        "color",
        "highlight_color",
        "effect",
        "alignment",
        "text_case",
        "letter_spacing",
        "line_spacing",
        "max_width_frac",
        "stroke_width",
        "position",
        "x_frac",
        "y_frac",
    }
)

_VALID_ALIGNMENT = {"left", "center", "right"}
_VALID_TEXT_CASE = {"none", "upper", "lower", "title"}
_VALID_POSITION = {"top", "middle", "bottom", "custom"}
_VALID_OVERLAY_POSITION = {"top", "center", "bottom", "custom"}
_VALID_OVERLAY_DISPLAY_MODE = {"pip", "fullscreen"}
_VALID_CAPTION_STYLE = {"sentence", "word"}
_VALID_OPEN_TOOLS = {"text", "visuals", "sounds", "overlays", "styles"}


def _load_motion_preset_params() -> dict[str, dict[str, tuple[str, int, int, int]]]:
    """Project the immutable Creator Block catalog into the Copilot contract."""
    source = Path(__file__).resolve()
    candidates = (
        Path("/app/motion-runtime/creator-blocks.catalog.json"),
        *(
            parent / "packages" / "motion-runtime" / "creator-blocks.catalog.json"
            for parent in source.parents
        ),
    )
    catalog_path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if catalog_path is None:
        raise RuntimeError("Creator Block catalog is missing")
    catalog = json.loads(catalog_path.read_text())
    rules: dict[str, dict[str, tuple[str, int, int, int]]] = {}
    for preset in catalog["presets"]:
        if not preset.get("ai_exposed"):
            continue
        preset_rules: dict[str, tuple[str, int, int, int]] = {}
        for parameter in preset["parameters"]:
            parameter_type = parameter["type"]
            key = "asset_ids" if parameter_type == "asset_list" else parameter["key"]
            kind = {
                "string": "text" if parameter["required"] else "optional_text",
                "string_list": "list",
                "asset_list": "assets",
            }[parameter_type]
            if parameter_type == "string":
                minimum = parameter.get("min_length", 0)
                maximum = parameter["max_length"]
                item_maximum = 0
            else:
                minimum = parameter["min_items"]
                maximum = parameter["max_items"]
                item_maximum = parameter.get("max_length", 0)
            preset_rules[key] = (kind, minimum, maximum, item_maximum)
        rules[preset["preset_id"]] = preset_rules
    return rules


_MOTION_PRESET_PARAMS = _load_motion_preset_params()
_OVERLAY_PATCH_FIELDS = frozenset(
    {
        "start_s",
        "end_s",
        "position",
        "x_frac",
        "y_frac",
        "scale",
        "display_mode",
    }
)
_CAPTION_META_FIELDS = frozenset(
    {
        "enabled",
        "style",
        "font",
        "y_frac",
        "size_px",
        "color",
        "highlight_color",
        "stroke_width",
        "shadow_enabled",
    }
)
_FONT_KIND_HINTS: dict[str, str] = {
    "playfair": "serif",
    "fraunces": "serif",
    "cormorant": "serif",
    "dm serif": "serif",
    "inter": "sans",
    "dm sans": "sans",
    "montserrat": "sans",
    "manrope": "sans",
    "bebas": "display",
    "anton": "display",
    "oswald": "display",
}


class EditCopilotInput(BaseModel):
    """Per-turn input for the editor copilot."""

    utterance: str = Field(default="", max_length=500)
    prior_turns: list[dict] = Field(default_factory=list, max_length=12)
    variant_snapshot: dict = Field(default_factory=dict)


class EditCopilotOutput(BaseModel):
    """Parsed edit intent and v1 editor operations."""

    intent: Literal["edit", "clarify", "describe", "reject", "unknown"]
    ops: list[dict] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reply: str
    suggestions: list[str] = Field(default_factory=list)
    needs_clarification: bool = False


def _clean_prompt_data(value: object, *, max_chars: int = 220) -> str:
    clean = _sanitize_text(str(value or ""))
    clean = re.sub(r"[\x00-\x1f\x7f]+", " ", clean)
    clean = clean.replace("{", "(").replace("}", ")").replace("$", "")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:max_chars]


def _snapshot_list(snapshot: dict, keys: Iterable[str]) -> list:
    for key in keys:
        value = snapshot.get(key)
        if isinstance(value, list):
            return value
    return []


def _snapshot_len(snapshot: dict, keys: Iterable[str]) -> int:
    return len(_snapshot_list(snapshot, keys))


def _slot_window(slot: dict) -> tuple[float | None, float | None]:
    start = _first_number(slot, ("output_start_s", "start_s", "start"))
    end = _first_number(slot, ("output_end_s", "end_s", "end"))
    if start is not None and end is not None:
        return start, end
    duration = _first_number(slot, ("duration_s", "duration"))
    if duration is not None:
        return start or 0.0, (start or 0.0) + duration
    return start, end


def _safe_finite_float(value: object) -> float | None:
    """Coerce an untrusted snapshot value to a finite float, or None.

    json.loads yields arbitrary-precision ints (float() can raise OverflowError)
    and accepts Infinity/NaN literals — none of which may reach the prompt.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _first_number(data: dict, keys: Iterable[str]) -> float | None:
    for key in keys:
        if key in data:
            try:
                return float(data[key])
            except (TypeError, ValueError, OverflowError):
                return None
    return None


def _format_prior_turns(turns: list[dict]) -> str:
    if not turns:
        return "(no prior turns)"
    lines: list[str] = []
    for turn in turns[:12]:
        role = _clean_prompt_data(turn.get("role", "unknown"), max_chars=30).upper()
        content = _clean_prompt_data(turn.get("content", ""), max_chars=350)
        if content:
            lines.append(f"{role}: {content}")
        applied = turn.get("applied") or []
        rejected = turn.get("rejected") or []
        if isinstance(applied, list) and applied:
            lines.append(f"  SYSTEM APPLIED: {_clean_prompt_data(applied, max_chars=500)}")
        if isinstance(rejected, list) and rejected:
            lines.append(f"  SYSTEM REJECTED: {_clean_prompt_data(rejected, max_chars=500)}")
    return "\n".join(lines) if lines else "(no prior turns)"


def _format_snapshot(snapshot: dict) -> str:
    if not isinstance(snapshot, dict) or not snapshot:
        return "(empty snapshot)"

    text_bars = _snapshot_list(snapshot, _TEXT_INDEX_KEYS)
    slots = _snapshot_list(snapshot, _SLOT_INDEX_KEYS)
    allowed = snapshot.get("allowed_op_families") or []
    has_captions = bool(snapshot.get("has_narrated_captions"))
    total_s = _first_number(snapshot, ("total_duration_s", "duration_s", "duration"))

    lines = [
        f"allowed_op_families: {', '.join(str(x) for x in allowed) if allowed else '(all v1 ops)'}",
        f"has_narrated_captions: {has_captions}",
    ]
    if total_s is not None:
        lines.append(f"total_duration_s: {total_s:.2f} (cap 60.00)")

    intro = snapshot.get("intro")
    if isinstance(intro, dict):
        lines.append("\nINTRO (layout re-render — not a draft edit):")
        lines.append(
            "layout="
            f"{_clean_prompt_data(intro.get('layout'), max_chars=40)!r} "
            "mode="
            f"{_clean_prompt_data(intro.get('mode'), max_chars=40)!r} "
            "word_count="
            f"{_clean_prompt_data(intro.get('word_count'), max_chars=20)!r}"
        )
        lines.append(
            "sequence_capable="
            f"{bool(intro.get('sequence_capable'))} "
            "cluster_eligible="
            f"{bool(intro.get('cluster_eligible'))} "
            "switch_blocked_reason="
            f"{_clean_prompt_data(intro.get('switch_blocked_reason'), max_chars=40)!r}"
        )
        lines.append(f"text={_clean_prompt_data(intro.get('text'), max_chars=300)!r}")

    carousel = snapshot.get("carousel")
    if isinstance(carousel, dict):
        lines.append(
            "\nCAROUSEL (Blossom carousel — full-screen multi-clip moment; "
            "re-render, not a draft edit):"
        )
        lines.append(
            "eligible="
            f"{bool(carousel.get('eligible'))} "
            "reason="
            f"{_clean_prompt_data(carousel.get('reason'), max_chars=80)!r} "
            "n_clips="
            f"{_clean_prompt_data(carousel.get('n_clips'), max_chars=10)!r}"
        )
        current = carousel.get("current")
        if isinstance(current, dict):
            lines.append(
                "current: position="
                f"{_clean_prompt_data(current.get('position'), max_chars=20)!r} "
                "mode="
                f"{_clean_prompt_data(current.get('mode'), max_chars=20)!r} "
                "effect="
                f"{_clean_prompt_data(current.get('effect'), max_chars=20)!r} "
                "focus_clip_index="
                f"{_clean_prompt_data(current.get('focus_clip_index'), max_chars=10)!r} "
                "duration_s="
                f"{_fmt_round3(_first_number(current, ('duration_s',)))} "
                "transition="
                f"{_clean_prompt_data(current.get('transition'), max_chars=20)!r}"
            )
        else:
            lines.append("current: (none — no carousel configured)")

    lines.append("\nTEXT BARS (indices are authoritative for this turn):")
    if text_bars:
        for i, bar in enumerate(text_bars):
            if not isinstance(bar, dict):
                continue
            text = _clean_prompt_data(bar.get("text", ""))
            start = _first_number(bar, ("start_s", "start"))
            end = _first_number(bar, ("end_s", "end"))
            style_bits = []
            for key in (
                "font_family",
                "size_px",
                "color",
                "highlight_color",
                "effect",
                "alignment",
                "position",
                "x_frac",
                "y_frac",
            ):
                if bar.get(key) is not None:
                    style_bits.append(f"{key}={_clean_prompt_data(bar.get(key), max_chars=80)}")
            timing = (
                f" {start:.2f}-{end:.2f}s"
                if start is not None and end is not None
                else " timing unknown"
            )
            lines.append(f"{i}. {timing} text={text!r} style: {', '.join(style_bits) or '(none)'}")
    else:
        lines.append("(none visible to copilot)")
    if has_captions:
        lines.append("Note: caption cue text/timing uses the CAPTIONS section below.")

    lines.append("\nCLIP SLOTS (indices are authoritative for this turn):")
    if slots:
        for i, slot in enumerate(slots):
            if not isinstance(slot, dict):
                continue
            duration = _first_number(slot, ("duration_s", "duration"))
            source = _first_number(slot, ("source_duration_s", "sourceDurationS"))
            in_s = _first_number(slot, ("in_s", "source_start_s", "sourceStartS"))
            start, end = _slot_window(slot)
            moment = _clean_prompt_data(slot.get("moment") or slot.get("label") or "")
            transition = _clean_prompt_data(
                slot.get("transition_after") or "cut",
                max_chars=30,
            )
            lines.append(
                f"{i}. output={_fmt_range(start, end)} duration={_fmt_num(duration)}s "
                f"in={_fmt_num(in_s)}s source={_fmt_num(source)}s moment={moment!r} "
                f"transition_after={transition!r} "
                f"transition_duration_s={_fmt_num(_first_number(slot, ('transition_duration_s',)))}"
            )
    else:
        lines.append("(none)")

    camera_effects = snapshot.get("camera_effects")
    if isinstance(camera_effects, list):
        lines.append("\nCAMERA EFFECTS (indices are authoritative for this turn):")
        if not camera_effects:
            lines.append("(none)")
        for i, effect in enumerate(camera_effects[:20]):
            if not isinstance(effect, dict):
                continue
            effect_range = _fmt_range(
                _first_number(effect, ("start_s",)),
                _first_number(effect, ("end_s",)),
            )
            lines.append(
                f"{i}. {effect_range} intensity={_fmt_num(_first_number(effect, ('intensity',)))}"
            )

    visual_blocks = snapshot.get("visual_blocks")
    if isinstance(visual_blocks, list):
        lines.append("\nVISUAL BLOCKS (indices are authoritative for this turn):")
        if not visual_blocks:
            lines.append("(none)")
        for i, block in enumerate(visual_blocks[:20]):
            if not isinstance(block, dict):
                continue
            lines.append(
                f"{i}. id={_clean_prompt_data(block.get('id'), max_chars=80)!r} "
                f"kind={_clean_prompt_data(block.get('kind'), max_chars=30)!r} "
                "time="
                f"{_fmt_range(_as_float(block.get('start_s')), _as_float(block.get('end_s')))} "
                f"transition_in={_clean_prompt_data(block.get('transition_in'), max_chars=20)!r} "
                f"transition_out={_clean_prompt_data(block.get('transition_out'), max_chars=20)!r}"
            )

    motion = snapshot.get("motion")
    if isinstance(motion, dict):
        catalog = motion.get("catalog") if isinstance(motion.get("catalog"), list) else []
        blocks = motion.get("blocks") if isinstance(motion.get("blocks"), list) else []
        assets = motion.get("asset_pool") if isinstance(motion.get("asset_pool"), list) else []
        lines.append("\nCREATOR BLOCK CATALOG (immutable IDs; copy preset_id exactly):")
        for entry in catalog[:8]:
            if not isinstance(entry, dict):
                continue
            default_params = _clean_prompt_data(
                json.dumps(entry.get("defaults") or {}, ensure_ascii=False),
                max_chars=240,
            )
            lines.append(
                f"- preset_id={_clean_prompt_data(entry.get('preset_id'), max_chars=40)!r} "
                f"label={_clean_prompt_data(entry.get('label'), max_chars=40)!r} "
                f"kind={_clean_prompt_data(entry.get('kind'), max_chars=20)!r} "
                f"default_duration_s={_fmt_round3(_first_number(entry, ('default_duration_s',)))} "
                f"min_assets={_clean_prompt_data(entry.get('min_assets'), max_chars=8)!r} "
                f"default_params={default_params!r}"
            )
        lines.append("EXISTING CREATOR BLOCKS (copy motion_id exactly for patch/remove):")
        if not blocks:
            lines.append("(none)")
        for block in blocks[:8]:
            if not isinstance(block, dict):
                continue
            params = _clean_prompt_data(
                json.dumps(block.get("params") or {}, ensure_ascii=False),
                max_chars=300,
            )
            lines.append(
                f"- motion_id={_clean_prompt_data(block.get('id'), max_chars=80)!r} "
                f"preset_id={_clean_prompt_data(block.get('preset_id'), max_chars=40)!r} "
                f"label={_clean_prompt_data(block.get('label'), max_chars=40)!r} "
                f"timing={_fmt_round3(_first_number(block, ('start_s',)))}-"
                f"{_fmt_round3(_first_number(block, ('end_s',)))}s "
                f"params={params!r}"
            )
        lines.append(
            "ELIGIBLE CREATOR BLOCK IMAGES (copy asset_ids exactly; never emit paths/URLs):"
        )
        if not assets:
            lines.append("(none)")
        for asset in assets[:20]:
            if isinstance(asset, dict):
                lines.append(
                    f"- id={_clean_prompt_data(asset.get('id'), max_chars=80)!r} "
                    f"subject={_clean_prompt_data(asset.get('subject'), max_chars=60)!r}"
                )

    beat_marks = snapshot.get("beat_marks")
    if isinstance(beat_marks, list):
        marks = [m for m in (_safe_finite_float(v) for v in beat_marks) if m is not None]
        if marks:
            shown = marks[:_BEAT_MARKS_SHOWN_MAX]
            intervals = sorted(b - a for a, b in zip(shown, shown[1:]) if b > a)
            lines.append(
                "\nMUSIC BEAT MARKS (assembled-timeline seconds; when beat-syncing, "
                "copy timing values exactly from this list):"
            )
            lines.append(", ".join(_fmt_round3(m) for m in shown))
            if intervals:
                # "listed marks", not "beats": the producer may stride-cap a
                # long grid, so consecutive listed marks can span several beats.
                lines.append(
                    f"median interval between listed marks: {intervals[len(intervals) // 2]:.3f}s"
                )

    speech = snapshot.get("speech")
    if isinstance(speech, dict):
        words = speech.get("words") if isinstance(speech.get("words"), list) else []
        pauses = speech.get("pauses") if isinstance(speech.get("pauses"), list) else []
        word_parts: list[str] = []
        for w in words[:_SPEECH_WORDS_SHOWN_MAX]:
            if not isinstance(w, dict):
                continue
            text = _clean_prompt_data(w.get("text") or w.get("w"), max_chars=40)
            start = _safe_finite_float(w.get("start_s", w.get("s")))
            end = _safe_finite_float(w.get("end_s", w.get("e")))
            if text and start is not None and end is not None:
                # repr-escaped like every other snapshot field — word text must
                # not be able to terminate its own quoted span in the prompt.
                word_parts.append(f"{text!r}@{start:.2f}-{end:.2f}")
        pause_parts: list[str] = []
        for p in pauses[:_PAUSE_MARKS_SHOWN_MAX]:
            if not isinstance(p, dict):
                continue
            start = _safe_finite_float(p.get("start_s", p.get("s")))
            end = _safe_finite_float(p.get("end_s", p.get("e")))
            if start is None or end is None:
                continue
            after = _clean_prompt_data(p.get("after"), max_chars=40)
            suffix = f' (after "{after}")' if after else " (before speech starts)"
            pause_parts.append(f"{start:.2f}-{end:.2f}{suffix}")
        if word_parts or pause_parts:
            source = _clean_prompt_data(speech.get("source"), max_chars=40)
            lines.append(
                "\nSPEECH WORDS (spoken words, assembled-timeline seconds; for "
                "word-precise placement copy timing values exactly from this list; "
                f"source={source}):"
            )
            # The producer may drop the word list under byte-budget pressure
            # while keeping pauses — pause placement stays possible.
            lines.append(", ".join(word_parts) if word_parts else "(word list trimmed for size)")
            lines.append(
                'PAUSE MARKS (silences between spoken words; "at the pause" means '
                "the pause's start time):"
            )
            lines.append("; ".join(pause_parts) if pause_parts else "(none detected)")

    if isinstance(snapshot.get("sfx"), dict):
        sfx = snapshot["sfx"]
        placements = sfx.get("placements") if isinstance(sfx.get("placements"), list) else []
        catalog = sfx.get("catalog") if isinstance(sfx.get("catalog"), list) else []
        lines.append("\nSFX PINS (sfx_index values are authoritative for this turn):")
        if placements:
            for placement in placements[:15]:
                if not isinstance(placement, dict):
                    continue
                placement_id = _clean_prompt_data(placement.get("id"), max_chars=80)
                label = _clean_prompt_data(placement.get("label"), max_chars=80)
                lines.append(
                    f"{placement.get('index')}. id={placement_id!r} "
                    f"label={label!r} "
                    f"at={_fmt_round3(_first_number(placement, ('at_s',)))}s "
                    f"gain={_fmt_round3(_first_number(placement, ('gain',)))} "
                    f"duration={_fmt_round3(_first_number(placement, ('duration_s',)))}s"
                )
        else:
            lines.append("(none)")
        lines.append("SFX CATALOG (use effect_id exactly as shown):")
        if catalog:
            for effect in catalog[:20]:
                if not isinstance(effect, dict):
                    continue
                roles = effect.get("role_tags")
                roles_part = ""
                if isinstance(roles, list) and roles:
                    clean_roles = [_clean_prompt_data(r, max_chars=40) for r in roles[:6]]
                    roles_part = f" roles={','.join(r for r in clean_roles if r)}"
                lines.append(
                    f"- id={_clean_prompt_data(effect.get('id'), max_chars=80)!r} "
                    f"name={_clean_prompt_data(effect.get('name'), max_chars=32)!r} "
                    f"duration={_fmt_round3(_first_number(effect, ('duration_s',)))}s"
                    f"{roles_part}"
                )
        else:
            lines.append("(none)")
        suggestions = sfx.get("suggestions") if isinstance(sfx.get("suggestions"), list) else []
        if suggestions:
            lines.append(
                "PENDING SFX SUGGESTIONS (advisory, from the auto sound-design pass; "
                "realize one by emitting add_sfx with exactly these values):"
            )
            for s in suggestions[:_SFX_SUGGESTIONS_SHOWN_MAX]:
                if not isinstance(s, dict):
                    continue
                lines.append(
                    f"- effect_id={_clean_prompt_data(s.get('effect_id'), max_chars=80)!r} "
                    f"at={_fmt_round3(_first_number(s, ('at_s',)))}s "
                    f"gain={_fmt_round3(_first_number(s, ('gain',)))} "
                    f"reason={_clean_prompt_data(s.get('reason'), max_chars=80)!r}"
                )

    if isinstance(snapshot.get("overlays"), dict):
        overlays = snapshot["overlays"]
        cards = overlays.get("cards") if isinstance(overlays.get("cards"), list) else []
        asset_pool = (
            overlays.get("asset_pool") if isinstance(overlays.get("asset_pool"), list) else []
        )
        suggestions = (
            overlays.get("pending_suggestions")
            if isinstance(overlays.get("pending_suggestions"), list)
            else []
        )
        lines.append("\nOVERLAY CARDS (overlay_index values are authoritative for this turn):")
        if cards:
            for card in cards[:12]:
                if not isinstance(card, dict):
                    continue
                lines.append(
                    f"{card.get('index')}. id={_clean_prompt_data(card.get('id'), max_chars=80)!r} "
                    f"kind={_clean_prompt_data(card.get('kind'), max_chars=40)!r} "
                    f"timing={_fmt_round3(_first_number(card, ('start_s',)))}-"
                    f"{_fmt_round3(_first_number(card, ('end_s',)))}s "
                    f"position={_clean_prompt_data(card.get('position'), max_chars=40)!r} "
                    f"x={_fmt_round3(_first_number(card, ('x_frac',)))} "
                    f"y={_fmt_round3(_first_number(card, ('y_frac',)))} "
                    f"scale={_fmt_round3(_first_number(card, ('scale',)))} "
                    f"display={_clean_prompt_data(card.get('display_mode'), max_chars=40)!r}"
                )
        else:
            lines.append("(none)")
        lines.append("ASSET POOL (use asset_id exactly as shown):")
        if asset_pool:
            for asset in asset_pool[:12]:
                if not isinstance(asset, dict):
                    continue
                lines.append(
                    f"- id={_clean_prompt_data(asset.get('id'), max_chars=80)!r} "
                    f"kind={_clean_prompt_data(asset.get('kind'), max_chars=40)!r} "
                    f"subject={_clean_prompt_data(asset.get('subject'), max_chars=60)!r} "
                    f"duration={_fmt_round3(_first_number(asset, ('duration_s',)))}s"
                )
        else:
            lines.append("(none)")
        lines.append("PENDING SUGGESTIONS (use suggestion_id exactly as shown):")
        if suggestions:
            for suggestion in suggestions[:6]:
                if not isinstance(suggestion, dict):
                    continue
                lines.append(
                    f"- id={_clean_prompt_data(suggestion.get('id'), max_chars=80)!r} "
                    f"reason={_clean_prompt_data(suggestion.get('reason'), max_chars=80)!r} "
                    f"timing={_fmt_round3(_first_number(suggestion, ('start_s',)))}-"
                    f"{_fmt_round3(_first_number(suggestion, ('end_s',)))}s"
                )
        else:
            lines.append("(none)")

    if isinstance(snapshot.get("captions"), dict):
        captions = snapshot["captions"]
        cues = captions.get("cues") if isinstance(captions.get("cues"), list) else []
        total_cues = captions.get("total_cues")
        truncated = bool(captions.get("truncated"))
        cues_editable = captions.get("cues_editable") is not False
        meta = captions.get("meta") if isinstance(captions.get("meta"), dict) else {}
        lines.append("\nCAPTIONS (cue_index values are authoritative for this turn):")
        if cues:
            for cue in cues[:40]:
                if not isinstance(cue, dict):
                    continue
                lines.append(
                    f"{cue.get('index')}. id={_clean_prompt_data(cue.get('id'), max_chars=80)!r} "
                    f"timing={_fmt_round3(_first_number(cue, ('start_s',)))}-"
                    f"{_fmt_round3(_first_number(cue, ('end_s',)))}s "
                    f"role={_clean_prompt_data(cue.get('smart_role'), max_chars=20)!r} "
                    f"emphasis={bool(cue.get('smart_emphasis'))} "
                    f"text={_clean_prompt_data(cue.get('text'), max_chars=80)!r}"
                )
        else:
            lines.append("(none)")
        lines.append(
            "caption_meta: "
            f"enabled={bool(meta.get('enabled'))} "
            f"style={_clean_prompt_data(meta.get('style'), max_chars=40)!r} "
            f"font={_clean_prompt_data(meta.get('font'), max_chars=80)!r} "
            f"y_frac={_fmt_round3(_first_number(meta, ('y_frac',)))} "
            f"size_px={_fmt_round3(_first_number(meta, ('size_px',)))} "
            f"color={_clean_prompt_data(meta.get('color'), max_chars=16)!r} "
            f"highlight={_clean_prompt_data(meta.get('highlight_color'), max_chars=16)!r} "
            f"stroke={_fmt_round3(_first_number(meta, ('stroke_width',)))} "
            f"shadow={meta.get('shadow_enabled')!r}"
        )
        if not cues_editable:
            lines.append(
                f"meta-only captions: {total_cues} transcript cues exist but their text "
                "and timing are not available in this draft — never emit "
                "edit_caption or set_caption_timing here. set_caption_meta "
                "(style/font/enabled/y_frac/size/color/stroke/shadow) DOES apply."
            )
        if truncated:
            lines.append(
                f"showing {len(cues[:40])} of {total_cues} cues; "
                "only listed indices are addressable"
            )

    if isinstance(snapshot.get("music"), dict):
        music = snapshot["music"]
        candidates = music.get("candidates") if isinstance(music.get("candidates"), list) else []
        lines.append("\nMUSIC:")
        lines.append(
            f"current_track_id={_clean_prompt_data(music.get('current_track_id'), max_chars=80)!r} "
            f"title={_clean_prompt_data(music.get('current_track_title'), max_chars=40)!r} "
            f"swappable={bool(music.get('swappable'))}"
        )
        lines.append("CANDIDATES (use track_id exactly as shown):")
        if candidates:
            for track in candidates[:20]:
                if not isinstance(track, dict):
                    continue
                lines.append(
                    f"- id={_clean_prompt_data(track.get('id'), max_chars=80)!r} "
                    f"title={_clean_prompt_data(track.get('title'), max_chars=40)!r}"
                )
        else:
            lines.append("(none)")

    if isinstance(snapshot.get("mix"), dict):
        mix = snapshot["mix"]
        lines.append("\nMIX:")
        lines.append(f"music_level={_fmt_round3(_first_number(mix, ('music_level',)))}")

    if "title" in snapshot:
        lines.append(f"\nTITLE: {_clean_prompt_data(snapshot.get('title'), max_chars=300)!r}")

    open_tools = snapshot.get("open_tools")
    if isinstance(open_tools, list):
        clean_tools = [_clean_prompt_data(tool, max_chars=30) for tool in open_tools]
        lines.append(f"\nOPENABLE TOOLS: {', '.join(clean_tools) if clean_tools else '(none)'}")

    return "\n".join(lines)


def _fmt_num(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.2f}"


def _fmt_range(start: float | None, end: float | None) -> str:
    if start is None or end is None:
        return "unknown"
    return f"{start:.2f}-{end:.2f}s"


def _round_snapshot_float(value: float) -> float:
    return round(float(value), 3)


def _fmt_round3(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{_round_snapshot_float(value):.3f}"


def _font_catalog() -> str:
    fonts = sorted(_ALLOWED_FONTS)
    lines = []
    for font in fonts:
        lower = font.lower()
        kind = next((tag for needle, tag in _FONT_KIND_HINTS.items() if needle in lower), "sans")
        lines.append(f"- {font} ({kind})")
    return "\n".join(lines)


def _effect_catalog() -> str:
    return "\n".join(f"- {effect}" for effect in sorted(_ALLOWED_EFFECTS))


def _caption_font_catalog() -> str:
    try:
        from app.pipeline.text_overlay import _FONT_REGISTRY  # noqa: PLC0415

        fonts = [
            name
            for name, entry in _FONT_REGISTRY.get("fonts", {}).items()
            if isinstance(name, str) and isinstance(entry, dict) and not entry.get("deprecated")
        ]
    except Exception:  # noqa: BLE001
        fonts = []
    if not fonts:
        return "- Fonts are validated at save time. Pass user-requested font names verbatim."
    return "\n".join(f"- {font}" for font in sorted(fonts))


class _ParseState:
    def __init__(self, confidence: float) -> None:
        self.confidence = confidence
        self.invalid_value_seen = False

    def invalid_value(self) -> None:
        self.invalid_value_seen = True
        self.confidence = min(self.confidence, 0.4)


class EditCopilotAgent(Agent[EditCopilotInput, EditCopilotOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.edit.copilot",
        prompt_id="edit_copilot",
        prompt_version=EDIT_COPILOT_PROMPT_VERSION,
        model=settings.edit_copilot_model,
        max_attempts=3,
        backoff_s=(2.0, 6.0),
        timeout_s=20.0,
        thinking_level="low",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = EditCopilotInput
    Output = EditCopilotOutput
    response_json = True

    def required_fields(self) -> list[str]:
        return ["intent", "reply"]

    def render_prompt(self, input: EditCopilotInput) -> str:  # noqa: A002
        return load_prompt(
            "edit_copilot",
            utterance=_clean_prompt_data(input.utterance[:500], max_chars=500),
            prior_turns=_format_prior_turns(input.prior_turns),
            snapshot=_format_snapshot(input.variant_snapshot),
            font_catalog=_font_catalog(),
            effect_catalog=_effect_catalog(),
            caption_font_catalog=_caption_font_catalog(),
        )

    def parse(self, raw_text: str, input: EditCopilotInput) -> EditCopilotOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"edit_copilot: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("edit_copilot: response is not a JSON object")

        intent = data.get("intent", "unknown")
        if intent not in _VALID_INTENTS:
            log.warning("edit_copilot.unknown_intent", intent=intent)
            intent = "unknown"

        confidence = _coerce_confidence(data.get("confidence", 0.5))
        state = _ParseState(confidence)
        raw_ops = data.get("ops") or []
        if not isinstance(raw_ops, list):
            raw_ops = []

        ops: list[dict] = []
        ordinary_op_count = 0
        bulk_caption_op_count = 0
        for raw_op in raw_ops:
            # A bulk caption replacement is one atomic client operation no
            # matter how many cues it changes. Keep it outside the ordinary
            # per-operation cap so a model that emits it after 12 unrelated
            # ops cannot silently turn "replace every occurrence" into a
            # false success receipt.
            raw_name = (
                str(raw_op.get("op") or raw_op.get("type") or "").strip()
                if isinstance(raw_op, dict)
                else ""
            )
            if raw_name == "replace_caption_text":
                if bulk_caption_op_count >= 1:
                    continue
            elif ordinary_op_count >= _MAX_OPS:
                continue
            parsed = _parse_op(raw_op, input.variant_snapshot, state)
            if parsed is not None:
                ops.append(parsed)
                if raw_name == "replace_caption_text":
                    bulk_caption_op_count += 1
                else:
                    ordinary_op_count += 1

        reply = str(data.get("reply") or "").strip()
        if not reply:
            reply = "Got it. What else should we change?"

        suggestions_raw = data.get("suggestions") or []
        if not isinstance(suggestions_raw, list):
            suggestions_raw = []
        suggestions = [str(s).strip() for s in suggestions_raw if str(s).strip()][:5]

        needs_clarification = bool(data.get("needs_clarification", False))
        if state.confidence < _CONFIDENCE_CLARIFY_THRESHOLD:
            needs_clarification = True

        try:
            return EditCopilotOutput(
                intent=intent,  # type: ignore[arg-type]
                ops=ops,
                confidence=state.confidence,
                reply=reply,
                suggestions=suggestions,
                needs_clarification=needs_clarification,
            )
        except Exception as exc:  # noqa: BLE001
            raise RefusalError(f"edit_copilot: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: return ONLY valid JSON with keys: intent "
            "(edit|clarify|describe|reject|unknown), ops (array of v1 op objects), "
            "confidence (float 0-1), reply (string), suggestions (list of short chips), "
            "needs_clarification (boolean). No markdown or prose outside JSON."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


def _coerce_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.5
    return max(0.0, min(1.0, confidence))


def _parse_op(raw_op: object, snapshot: dict, state: _ParseState) -> dict | None:
    if not isinstance(raw_op, dict):
        log.warning("edit_copilot.drop_non_object_op", op=raw_op)
        return None

    name = str(raw_op.get("op") or raw_op.get("type") or "").strip()
    if name not in _VALID_OPS:
        log.warning("edit_copilot.drop_unknown_op", op=name)
        return None
    if not _family_allowed(name, snapshot):
        log.warning("edit_copilot.drop_disallowed_family", op=name)
        return None

    raw_payload = raw_op.get("payload")
    if isinstance(raw_payload, dict):
        payload = dict(raw_payload)
    else:
        payload = {k: v for k, v in raw_op.items() if k not in {"op", "type"}}
    payload = {k: v for k, v in payload.items() if k in _OP_FIELDS[name]}

    if name in {"set_text_timing", "set_caption_timing"} and not (
        {"start_s", "end_s"} & payload.keys()
    ):
        log.warning("edit_copilot.drop_missing_timing_bound")
        return None
    if name == "patch_sfx" and not ({"at_s", "gain"} & payload.keys()):
        log.warning("edit_copilot.drop_missing_sfx_patch")
        return None
    missing = _OP_REQUIRED[name] - payload.keys()
    if missing:
        log.warning("edit_copilot.drop_missing_fields", op=name, missing=sorted(missing))
        return None

    if not _indices_valid(name, payload, snapshot):
        return None

    parsed = _coerce_payload(name, payload, snapshot, state)
    if parsed is None:
        return None
    return {"op": name, **parsed}


def _family_allowed(name: str, snapshot: dict) -> bool:
    raw_allowed = snapshot.get("allowed_op_families") if isinstance(snapshot, dict) else None
    if raw_allowed in (None, []):
        return True
    if not isinstance(raw_allowed, list):
        return False
    allowed = {str(x).strip().lower() for x in raw_allowed if str(x).strip()}
    if name in allowed or "all" in allowed:
        return True
    if name in _TEXT_OPS:
        aliases = {"text", "text_timeline"}
    elif name in _STYLE_OPS:
        aliases = {"style", "text", "text_style"}
    elif name == "split_clip":
        aliases = {"clip", "clips", "timeline", "split_clips"}
    elif name in _SFX_OPS:
        aliases = {"sfx", "sound_effects", "sounds"}
    elif name in _OVERLAY_OPS:
        aliases = {"overlay", "overlays", "media"}
    elif name in _CAPTION_OPS:
        aliases = {"caption", "captions"}
    elif name == "set_mix":
        aliases = {"music", "audio", "mix"}
    elif name in _MUSIC_OPS:
        aliases = {"music", "audio"}
    elif name == "set_intro_layout":
        aliases = {"render", "layout", "intro_layout"}
    elif name in _RENDER_OPS:
        aliases = {"render", "carousel", "carousel_moment"}
    elif name in _TITLE_OPS:
        aliases = {"title"}
    elif name in _TOOL_OPS:
        aliases = {"tool", "open_tool", "navigation"}
    elif name in _EFFECT_OPS:
        aliases = {"effect", "effects", "camera_effects"}
    elif name in _TRANSITION_OPS:
        aliases = {"transition", "transitions", "timeline"}
    elif name in _VISUAL_OPS:
        aliases = {"visual", "visuals", "visual_blocks"}
    elif name in _MOTION_OPS:
        aliases = {"motion", "creator_blocks", "blocks"}
    else:
        aliases = {"clip", "clips", "timeline"}
    return bool(allowed & aliases)


def _indices_valid(name: str, payload: dict, snapshot: dict) -> bool:
    text_bars = _snapshot_list(snapshot, _TEXT_INDEX_KEYS)
    text_count = len(text_bars)
    slot_count = _snapshot_len(snapshot, _SLOT_INDEX_KEYS)
    sfx_count = _section_len(snapshot, "sfx", "placements")
    overlay_count = _section_len(snapshot, "overlays", "cards")
    cue_count = _section_len(snapshot, "captions", "cues")
    camera_effect_count = len(snapshot.get("camera_effects") or [])
    visual_block_count = len(snapshot.get("visual_blocks") or [])
    transition_slot_count = len(
        [
            slot
            for slot in _snapshot_list(snapshot, _SLOT_INDEX_KEYS)
            if isinstance(slot, dict) and not slot.get("removed")
        ]
    )
    for key in ("bar_index",):
        if key in payload and not _index_in_bounds(payload[key], text_count):
            log.warning("edit_copilot.drop_text_index_oob", op=name, index=payload.get(key))
            return False
        if key in payload and name in {"set_text_timing", "remove_text"}:
            bar = text_bars[int(payload[key])]
            if isinstance(bar, dict) and str(bar.get("id") or "").startswith("lyric_"):
                log.warning("edit_copilot.drop_locked_lyric_text_op", op=name, index=payload[key])
                return False
    for key in ("slot_index", "from_index", "to_index"):
        if key in payload and not _index_in_bounds(payload[key], slot_count):
            log.warning(
                "edit_copilot.drop_slot_index_oob",
                op=name,
                key=key,
                index=payload.get(key),
            )
            return False
    if "sfx_index" in payload and not _index_in_bounds(payload["sfx_index"], sfx_count):
        log.warning("edit_copilot.drop_sfx_index_oob", op=name, index=payload.get("sfx_index"))
        return False
    if "overlay_index" in payload and not _index_in_bounds(payload["overlay_index"], overlay_count):
        log.warning(
            "edit_copilot.drop_overlay_index_oob",
            op=name,
            index=payload.get("overlay_index"),
        )
        return False
    if "cue_index" in payload and not _index_in_bounds(payload["cue_index"], cue_count):
        log.warning("edit_copilot.drop_cue_index_oob", op=name, index=payload.get("cue_index"))
        return False
    if "camera_effect_index" in payload and not _index_in_bounds(
        payload["camera_effect_index"], camera_effect_count
    ):
        log.warning(
            "edit_copilot.drop_camera_effect_index_oob",
            op=name,
            index=payload.get("camera_effect_index"),
        )
        return False
    if "boundary_index" in payload and not _index_in_bounds(
        payload["boundary_index"], max(0, transition_slot_count - 1)
    ):
        log.warning(
            "edit_copilot.drop_boundary_index_oob",
            op=name,
            index=payload.get("boundary_index"),
        )
        return False
    if "visual_block_index" in payload and not _index_in_bounds(
        payload["visual_block_index"], visual_block_count
    ):
        log.warning(
            "edit_copilot.drop_visual_block_index_oob",
            op=name,
            index=payload.get("visual_block_index"),
        )
        return False
    return True


def _section_list(snapshot: dict, section: str, key: str) -> list:
    value = snapshot.get(section) if isinstance(snapshot, dict) else None
    if not isinstance(value, dict):
        return []
    items = value.get(key)
    return items if isinstance(items, list) else []


def _section_len(snapshot: dict, section: str, key: str) -> int:
    return len(_section_list(snapshot, section, key))


def _index_in_bounds(value: object, count: int) -> bool:
    if count <= 0:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return False
    if isinstance(value, str) and str(idx) != value.strip():
        return False
    return 0 <= idx < count


def _coerce_payload(
    name: str,
    payload: dict,
    snapshot: dict,
    state: _ParseState,
) -> dict | None:
    out = dict(payload)

    for key in (
        "bar_index",
        "slot_index",
        "from_index",
        "to_index",
        "sfx_index",
        "overlay_index",
        "cue_index",
        "camera_effect_index",
        "boundary_index",
        "visual_block_index",
    ):
        if key in out:
            try:
                out[key] = int(out[key])
            except (TypeError, ValueError):
                return None

    if name in {"edit_text", "add_text", "edit_caption"}:
        text = _clean_user_text(out.get("text"))
        if text is None:
            state.invalid_value()
            return None
        out["text"] = text

    if name == "replace_caption_text":
        captions = snapshot.get("captions")
        if isinstance(captions, dict) and captions.get("cues_editable") is False:
            state.invalid_value()
            return None
        find = _clean_user_text(out.get("find"))
        replace = _clean_caption_replacement(out.get("replace"))
        if find is None or replace is None:
            state.invalid_value()
            return None
        out["find"] = find
        out["replace"] = replace

    if name == "set_title":
        title = _clean_user_text(out.get("title"), max_chars=300)
        if title is None:
            state.invalid_value()
            return None
        out["title"] = title

    if name == "patch_text_style":
        patch = out.get("patch")
        if not isinstance(patch, dict):
            return None
        clean_patch = _coerce_patch(patch, state)
        if not clean_patch:
            return None
        out["patch"] = clean_patch

    if name == "patch_overlay":
        patch = out.get("patch")
        if not isinstance(patch, dict):
            return None
        clean_patch = _coerce_overlay_patch(patch, state)
        if not clean_patch:
            return None
        out["patch"] = clean_patch

    if name == "set_caption_meta":
        patch = out.get("patch")
        if not isinstance(patch, dict):
            return None
        clean_patch = _coerce_caption_meta_patch(patch, state)
        if not clean_patch:
            return None
        out["patch"] = clean_patch

    if name == "set_caption_emphasis":
        if not isinstance(out.get("emphasis"), bool):
            state.invalid_value()
            return None

    for key in (
        "start_s",
        "end_s",
        "in_s",
        "duration_s",
        "at_s",
        "gain",
        "music_level",
        "scale",
        "intensity",
    ):
        if key in out:
            try:
                out[key] = float(out[key])
            except (TypeError, ValueError):
                state.invalid_value()
                return None

    if "duration_s" in out:
        if out["duration_s"] <= 0:
            state.invalid_value()
            return None
    if "in_s" in out:
        out["in_s"] = max(0.0, out["in_s"])
    for key in ("start_s", "end_s", "at_s"):
        if key in out:
            out[key] = max(0.0, out[key])
    for key in ("x_frac", "y_frac"):
        if key in out:
            num = _as_float(out[key])
            if num is None:
                state.invalid_value()
                return None
            out[key] = max(0.0, min(1.0, num))
    if "scale" in out:
        out["scale"] = max(0.05, min(1.0, out["scale"]))
    if "gain" in out:
        out["gain"] = max(0.0, min(2.0, out["gain"]))
    if name == "add_sfx" and "gain" not in out:
        out["gain"] = 1.0
    if "music_level" in out:
        out["music_level"] = max(0.0, min(1.0, out["music_level"]))
    if "intensity" in out:
        out["intensity"] = (
            max(0.0, min(1.0, out["intensity"]))
            if name in _MOTION_OPS
            else max(0.01, min(0.08, out["intensity"]))
        )

    if name in _SFX_OPS and "at_s" in out:
        total_s = _first_number(snapshot, ("total_duration_s", "duration_s", "duration"))
        # total <= 0 = unknown duration (slot-less subtitled variant) — clamping
        # against it collapses every placement to 0.0s. Skip the upper clamp.
        if total_s is not None and total_s > 0:
            out["at_s"] = min(out["at_s"], max(0.0, total_s - 0.1))

    if name in {"add_overlay"}:
        for key in ("position", "display_mode"):
            if key in out and not isinstance(out[key], str):
                state.invalid_value()
                return None
        if "position" in out and out["position"] not in _VALID_OVERLAY_POSITION:
            state.invalid_value()
            return None
        if "display_mode" in out and out["display_mode"] not in _VALID_OVERLAY_DISPLAY_MODE:
            state.invalid_value()
            return None

    if (
        name in {"set_text_timing", "add_text", "set_caption_timing", "add_overlay"}
        and out.get("start_s") is not None
        and out.get("end_s") is not None
    ):
        if out["end_s"] <= out["start_s"]:
            state.invalid_value()
            return None

    if name == "add_sfx" and not _id_in_section(
        out.get("effect_id"), snapshot, "sfx", "catalog", "id"
    ):
        state.invalid_value()
        return None
    if name == "add_overlay" and not _id_in_section(
        out.get("asset_id"), snapshot, "overlays", "asset_pool", "id"
    ):
        state.invalid_value()
        return None
    if name == "accept_overlay_suggestion" and not _id_in_section(
        out.get("suggestion_id"), snapshot, "overlays", "pending_suggestions", "id"
    ):
        state.invalid_value()
        return None
    if name == "swap_music":
        music = snapshot.get("music") if isinstance(snapshot, dict) else None
        if not isinstance(music, dict) or music.get("swappable") is not True:
            state.invalid_value()
            return None
        if not _id_in_section(out.get("track_id"), snapshot, "music", "candidates", "id"):
            state.invalid_value()
            return None
    if name == "set_mix" and not isinstance(snapshot.get("mix"), dict):
        state.invalid_value()
        return None
    if name == "open_tool":
        tool = out.get("tool")
        if tool not in _VALID_OPEN_TOOLS:
            state.invalid_value()
            return None
        open_tools = snapshot.get("open_tools") if isinstance(snapshot, dict) else None
        if not isinstance(open_tools, list) or tool not in {str(item) for item in open_tools}:
            state.invalid_value()
            return None

    if name == "set_intro_layout":
        layout = out.get("layout")
        if layout not in {"linear", "cluster"}:
            state.invalid_value()
            return None
        intro = snapshot.get("intro") if isinstance(snapshot, dict) else None
        if not isinstance(intro, dict):
            log.warning("edit_copilot.drop_missing_intro_section")
            return None
        if layout == intro.get("layout"):
            return None
        if layout == "cluster" and intro.get("cluster_eligible") is not True:
            state.invalid_value()
            return None
        out["layout"] = layout

    if name == "split_clip":
        slots = _snapshot_list(snapshot, _SLOT_INDEX_KEYS)
        slot = slots[out["slot_index"]] if out["slot_index"] < len(slots) else {}
        if isinstance(slot, dict):
            start, end = _slot_window(slot)
            if start is not None and end is not None and not (start < out["at_s"] < end):
                state.invalid_value()
                return None

    if name in {"add_camera_effect", "patch_camera_effect"}:
        total_s = _first_number(snapshot, ("total_duration_s", "duration_s", "duration"))
        if total_s is not None and total_s > 0:
            for key in ("start_s", "end_s"):
                if key in out:
                    out[key] = min(out[key], total_s)
        start_s = out.get("start_s")
        end_s = out.get("end_s")
        if name == "patch_camera_effect":
            effects = _snapshot_list(snapshot, ("camera_effects",))
            effect_index = out.get("camera_effect_index")
            current = (
                effects[effect_index]
                if isinstance(effect_index, int) and effect_index < len(effects)
                else {}
            )
            if isinstance(current, dict):
                start_s = start_s if start_s is not None else _as_float(current.get("start_s"))
                end_s = end_s if end_s is not None else _as_float(current.get("end_s"))
        if start_s is not None and end_s is not None and end_s <= start_s:
            state.invalid_value()
            return None
        if name == "add_camera_effect" and "intensity" not in out:
            out["intensity"] = 0.04

    if name == "set_transition":
        if out.get("transition") not in {"cut", "crossfade", "dip_to_black", "flash"}:
            state.invalid_value()
            return None
        slots = [
            slot
            for slot in _snapshot_list(snapshot, _SLOT_INDEX_KEYS)
            if isinstance(slot, dict) and not slot.get("removed")
        ]
        boundary_index = out["boundary_index"]
        left = slots[boundary_index] if boundary_index < len(slots) else {}
        right = slots[boundary_index + 1] if boundary_index + 1 < len(slots) else {}
        if not isinstance(left, dict) or not isinstance(right, dict):
            state.invalid_value()
            return None
        left_start, left_end = _slot_window(left) if isinstance(left, dict) else (None, None)
        right_start, right_end = _slot_window(right) if isinstance(right, dict) else (None, None)
        adjacent_durations = [
            end - start
            for start, end in ((left_start, left_end), (right_start, right_end))
            if start is not None and end is not None
        ]
        if len(adjacent_durations) != 2:
            state.invalid_value()
            return None
        max_duration = min([0.3, *(duration * 0.3 for duration in adjacent_durations)])
        if out["transition"] != "cut" and max_duration < 0.1:
            state.invalid_value()
            return None
        requested_duration = max(0.1, float(out.get("duration_s") or 0.3))
        out["duration_s"] = (
            0.3 if out["transition"] == "cut" else min(0.3, max_duration, requested_duration)
        )

    if name == "set_visual_fade":
        if "transition_in" not in out and "transition_out" not in out:
            state.invalid_value()
            return None
        for key in ("transition_in", "transition_out"):
            if key in out and out[key] not in {"cut", "fade"}:
                state.invalid_value()
                return None

    if name in _MOTION_OPS:
        motion = snapshot.get("motion") if isinstance(snapshot, dict) else None
        if not isinstance(motion, dict) or motion.get("available") is not True:
            state.invalid_value()
            return None
        blocks = motion.get("blocks") if isinstance(motion.get("blocks"), list) else []
        current = None
        if name != "add_motion_block":
            motion_id = out.get("motion_id")
            current = next(
                (
                    block
                    for block in blocks
                    if isinstance(block, dict) and block.get("id") == motion_id
                ),
                None,
            )
            if current is None:
                state.invalid_value()
                return None
        preset_id = out.get("preset_id") if name == "add_motion_block" else current.get("preset_id")
        catalog_ids = {
            entry.get("preset_id") for entry in motion.get("catalog", []) if isinstance(entry, dict)
        }
        if name == "add_motion_block" and preset_id not in catalog_ids:
            state.invalid_value()
            return None
        if name == "patch_motion_block":
            patch = out.get("patch")
            if not isinstance(patch, dict) or not patch:
                state.invalid_value()
                return None
            allowed_patch = {"start_s", "end_s", "palette", "intensity", "params"}
            if any(key not in allowed_patch for key in patch):
                state.invalid_value()
                return None
            clean_patch: dict[str, Any] = {}
            for key in ("start_s", "end_s", "intensity"):
                if key in patch:
                    value = _as_float(patch[key])
                    if value is None:
                        state.invalid_value()
                        return None
                    clean_patch[key] = (
                        max(0.0, min(1.0, value)) if key == "intensity" else max(0.0, value)
                    )
            if "palette" in patch:
                palette = _clean_motion_palette(patch["palette"])
                if palette is None:
                    state.invalid_value()
                    return None
                clean_patch["palette"] = palette
            if "params" in patch:
                params = _clean_motion_params(
                    str(preset_id), patch["params"], snapshot, partial=True
                )
                if params is None:
                    state.invalid_value()
                    return None
                clean_patch["params"] = params
            start_s = clean_patch.get("start_s", _as_float(current.get("start_s")))
            end_s = clean_patch.get("end_s", _as_float(current.get("end_s")))
            if not _valid_motion_timing(start_s, end_s, snapshot):
                state.invalid_value()
                return None
            out["patch"] = clean_patch
        elif name == "add_motion_block":
            params = _clean_motion_params(
                str(preset_id), out.get("params"), snapshot, partial=False
            )
            if params is None or not _valid_motion_timing(
                out.get("start_s"), out.get("end_s"), snapshot
            ):
                state.invalid_value()
                return None
            out["params"] = params
            if "palette" in out:
                palette = _clean_motion_palette(out["palette"])
                if palette is None:
                    state.invalid_value()
                    return None
                out["palette"] = palette
            if "intensity" not in out:
                out["intensity"] = 0.72
        if not _motion_active_union_valid(name, out, blocks):
            state.invalid_value()
            return None

    if name == "set_carousel_moment":
        carousel = snapshot.get("carousel") if isinstance(snapshot, dict) else None
        if not isinstance(carousel, dict):
            log.warning("edit_copilot.drop_missing_carousel_section")
            return None
        current = carousel.get("current") if isinstance(carousel.get("current"), dict) else None
        config = out.get("config")
        if config is None:
            # Explicit removal is always allowed — mirrors dispatch_edit_variant's
            # "no eligibility check on removal" rule (a moment can need clearing
            # even after a flag flip or an archetype change that would otherwise
            # reject an add).
            if current is None:
                return None  # no-op: nothing to remove
            out["config"] = None
            return out
        if not isinstance(config, dict):
            state.invalid_value()
            return None
        if carousel.get("eligible") is not True:
            log.warning(
                "edit_copilot.drop_ineligible_carousel",
                reason=carousel.get("reason"),
            )
            return None
        cleaned: dict[str, Any] = {}
        if "position" in config:
            if config["position"] not in {"intro", "middle", "outro"}:
                state.invalid_value()
                return None
            cleaned["position"] = config["position"]
        if "mode" in config:
            # "stills" is a legal persisted value (auto-authored moments) but is
            # NEVER a valid copilot write — the vocabulary below intentionally
            # excludes it (mirrors CarouselMoment on the web side).
            if config["mode"] not in {"focus", "rolling"}:
                state.invalid_value()
                return None
            cleaned["mode"] = config["mode"]
        if "effect" in config:
            if config["effect"] not in {"scale_sweep", "cover_flow", "cards_stack", "flipbook"}:
                state.invalid_value()
                return None
            cleaned["effect"] = config["effect"]
        if "focus_clip_index" in config:
            idx = config["focus_clip_index"]
            if idx is not None:
                if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
                    state.invalid_value()
                    return None
            cleaned["focus_clip_index"] = idx
        if "duration_s" in config:
            duration = _as_float(config["duration_s"])
            if duration is None:
                state.invalid_value()
                return None
            cleaned["duration_s"] = max(2.0, min(15.0, duration))
        if "transition" in config:
            if config["transition"] not in {"crossfade", "none"}:
                state.invalid_value()
                return None
            cleaned["transition"] = config["transition"]
        if not cleaned:
            state.invalid_value()
            return None
        # No-op detection: every field the model proposed already matches the
        # persisted moment (config is a partial patch, so only compare the
        # fields actually present).
        if current is not None and all(
            key in current and current[key] == value for key, value in cleaned.items()
        ):
            return None
        out["config"] = cleaned

    return out


def _id_in_section(value: object, snapshot: dict, section: str, list_key: str, id_key: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return value in {
        str(item.get(id_key))
        for item in _section_list(snapshot, section, list_key)
        if isinstance(item, dict) and item.get(id_key) is not None
    }


def _clean_motion_palette(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict) or set(value) != {"primary", "accent"}:
        return None
    primary = value.get("primary")
    accent = value.get("accent")
    if not isinstance(primary, str) or not isinstance(accent, str):
        return None
    if not _HEX_COLOR_RE.fullmatch(primary) or not _HEX_COLOR_RE.fullmatch(accent):
        return None
    return {"primary": primary.upper(), "accent": accent.upper()}


def _clean_motion_params(
    preset_id: str,
    value: object,
    snapshot: dict,
    *,
    partial: bool,
) -> dict[str, Any] | None:
    rules = _MOTION_PRESET_PARAMS.get(preset_id)
    if rules is None or not isinstance(value, dict) or any(key not in rules for key in value):
        return None
    required = {key for key, (kind, _, _, _) in rules.items() if kind != "optional_text"}
    if not partial and not required.issubset(value):
        return None
    if partial and not value:
        return None
    eligible_assets = {
        str(item.get("id"))
        for item in _section_list(snapshot, "motion", "asset_pool")
        if isinstance(item, dict) and item.get("id") is not None
    }
    cleaned: dict[str, Any] = {}
    for key, raw in value.items():
        kind, minimum, maximum, item_maximum = rules[key]
        if kind in {"text", "optional_text"}:
            text = _clean_user_text(raw, max_chars=maximum)
            if text is None or len(text) < minimum:
                return None
            cleaned[key] = text
        elif kind == "list":
            if not isinstance(raw, list) or not minimum <= len(raw) <= maximum:
                return None
            items = [_clean_user_text(item, max_chars=item_maximum) for item in raw]
            if any(item is None for item in items):
                return None
            cleaned[key] = items
        elif kind == "assets":
            if not isinstance(raw, list) or not minimum <= len(raw) <= maximum:
                return None
            if any(not isinstance(item, str) or item not in eligible_assets for item in raw):
                return None
            if len(set(raw)) != len(raw):
                return None
            cleaned[key] = list(raw)
    return cleaned


def _valid_motion_timing(start: object, end: object, snapshot: dict) -> bool:
    start_s = _as_float(start)
    end_s = _as_float(end)
    if start_s is None or end_s is None or start_s < 0 or end_s <= start_s:
        return False
    if end_s - start_s > 8.0 + 1e-6:
        return False
    total_s = _first_number(snapshot, ("total_duration_s", "duration_s", "duration"))
    return total_s is None or total_s <= 0 or end_s <= total_s + 1e-6


def _motion_active_union_valid(name: str, payload: dict, blocks: list) -> bool:
    intervals: list[tuple[float, float]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if name == "remove_motion_block" and block.get("id") == payload.get("motion_id"):
            continue
        start = _as_float(block.get("start_s"))
        end = _as_float(block.get("end_s"))
        if name == "patch_motion_block" and block.get("id") == payload.get("motion_id"):
            patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else {}
            start = _as_float(patch.get("start_s")) if "start_s" in patch else start
            end = _as_float(patch.get("end_s")) if "end_s" in patch else end
        if start is not None and end is not None and end > start:
            intervals.append((start, end))
    if name == "add_motion_block":
        if len(intervals) >= 8:
            return False
        start = _as_float(payload.get("start_s"))
        end = _as_float(payload.get("end_s"))
        if start is None or end is None:
            return False
        intervals.append((start, end))
    intervals.sort()
    total = 0.0
    current_start: float | None = None
    current_end: float | None = None
    for start, end in intervals:
        if current_start is None:
            current_start, current_end = start, end
        elif start <= (current_end or start):
            current_end = max(current_end or end, end)
        else:
            total += (current_end or current_start) - current_start
            current_start, current_end = start, end
    if current_start is not None:
        total += (current_end or current_start) - current_start
    return total <= 8.0 + 1e-6


def _clean_user_text(value: object, *, max_chars: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    clean = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    clean = re.sub(r"\s+", " ", clean)
    if not clean:
        return None
    return clean[:max_chars]


def _clean_caption_replacement(value: object, *, max_chars: int = 500) -> str | None:
    """Sanitize replacement text while preserving the valid empty string.

    Empty replacement means delete every literal match. It is distinct from a
    missing/non-string field, which remains invalid.
    """
    if not isinstance(value, str):
        return None
    clean = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    clean = re.sub(r"\s+", " ", clean)
    return clean[:max_chars]


def _coerce_patch(patch: dict, state: _ParseState) -> dict:
    out: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in _STYLE_PATCH_FIELDS:
            continue
        if key == "font_family":
            if not isinstance(value, str) or value not in _ALLOWED_FONTS:
                state.invalid_value()
                return {}
            out[key] = value
        elif key == "effect":
            if not isinstance(value, str) or value not in _ALLOWED_EFFECTS:
                state.invalid_value()
                return {}
            out[key] = value
        elif key in {"color", "highlight_color"}:
            if not isinstance(value, str) or not _HEX_COLOR_RE.match(value):
                state.invalid_value()
                return {}
            out[key] = value
        elif key == "alignment":
            if value not in _VALID_ALIGNMENT:
                state.invalid_value()
                return {}
            out[key] = value
        elif key == "text_case":
            if value not in _VALID_TEXT_CASE:
                state.invalid_value()
                return {}
            out[key] = value
        elif key == "position":
            if value not in _VALID_POSITION:
                state.invalid_value()
                return {}
            out[key] = value
        elif key == "size_px":
            num = _as_float(value)
            if num is None:
                state.invalid_value()
                return {}
            out[key] = max(8.0, min(300.0, num))
        elif key == "stroke_width":
            num = _as_float(value)
            if num is None:
                state.invalid_value()
                return {}
            out[key] = max(0.0, min(20.0, num))
        elif key in {"x_frac", "y_frac"}:
            num = _as_float(value)
            if num is None:
                state.invalid_value()
                return {}
            out[key] = max(0.0, min(1.0, num))
        elif key == "letter_spacing":
            num = _as_float(value)
            if num is None:
                state.invalid_value()
                return {}
            out[key] = max(-0.05, min(0.5, num))
        elif key == "line_spacing":
            num = _as_float(value)
            if num is None:
                state.invalid_value()
                return {}
            out[key] = max(0.5, min(3.0, num))
        elif key == "max_width_frac":
            num = _as_float(value)
            if num is None:
                state.invalid_value()
                return {}
            out[key] = max(0.2, min(1.0, num))
    return out


def _coerce_overlay_patch(patch: dict, state: _ParseState) -> dict:
    out: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in _OVERLAY_PATCH_FIELDS:
            continue
        if key in {"start_s", "end_s"}:
            num = _as_float(value)
            if num is None:
                state.invalid_value()
                return {}
            out[key] = max(0.0, num)
        elif key == "position":
            if value not in _VALID_OVERLAY_POSITION:
                state.invalid_value()
                return {}
            out[key] = value
        elif key in {"x_frac", "y_frac"}:
            num = _as_float(value)
            if num is None:
                state.invalid_value()
                return {}
            out[key] = max(0.0, min(1.0, num))
        elif key == "scale":
            num = _as_float(value)
            if num is None:
                state.invalid_value()
                return {}
            out[key] = max(0.05, min(1.0, num))
        elif key == "display_mode":
            if value not in _VALID_OVERLAY_DISPLAY_MODE:
                state.invalid_value()
                return {}
            out[key] = value
    if out.get("start_s") is not None and out.get("end_s") is not None:
        if out["end_s"] <= out["start_s"]:
            state.invalid_value()
            return {}
    return out


def _coerce_caption_meta_patch(patch: dict, state: _ParseState) -> dict:
    out: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in _CAPTION_META_FIELDS:
            continue
        if key == "enabled":
            if not isinstance(value, bool):
                state.invalid_value()
                return {}
            out[key] = value
        elif key == "style":
            if value not in _VALID_CAPTION_STYLE:
                state.invalid_value()
                return {}
            out[key] = value
        elif key == "font":
            if value is None:
                out[key] = None
            elif isinstance(value, str) and value.strip():
                out[key] = value.strip()
            else:
                state.invalid_value()
                return {}
        elif key == "y_frac":
            from app.pipeline.captions import clamp_caption_y_frac  # noqa: PLC0415

            num = _as_float(value)
            if num is None:
                state.invalid_value()
                return {}
            out[key] = clamp_caption_y_frac(num)
        elif key == "size_px":
            num = _as_float(value)
            if num is None:
                state.invalid_value()
                return {}
            out[key] = max(36, min(160, int(round(num))))
        elif key in {"color", "highlight_color"}:
            if not isinstance(value, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", value.strip()):
                state.invalid_value()
                return {}
            out[key] = value.strip().upper()
        elif key == "stroke_width":
            num = _as_float(value)
            if num is None:
                state.invalid_value()
                return {}
            out[key] = max(0, min(12, int(round(num))))
        elif key == "shadow_enabled":
            if not isinstance(value, bool):
                state.invalid_value()
                return {}
            out[key] = value
    return out


def _as_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Public shared editor-operation contract used by the proactive Director.
# Copilot prompt/retry behavior stays private to this module; these wrappers
# are the stable validation and snapshot-formatting boundary.
EditorOperationParseState = _ParseState


def clean_editor_prompt_data(value: object, *, max_chars: int = 220) -> str:
    return _clean_prompt_data(value, max_chars=max_chars)


def editor_effect_catalog() -> str:
    return _effect_catalog()


def editor_font_catalog() -> str:
    return _font_catalog()


def editor_operation_contract(snapshot: dict) -> str:
    """Return field-exact examples for only the operation families available now."""
    examples = [
        f"- {name}: {example}"
        for name, example in _DIRECTOR_OPERATION_EXAMPLES
        if _family_allowed(name, snapshot)
    ]
    return "\n".join(examples) if examples else "(no instant draft operations available)"


def format_editor_snapshot(snapshot: dict) -> str:
    return _format_snapshot(snapshot)


def parse_editor_operation(
    raw: object,
    snapshot: dict,
    state: EditorOperationParseState,
) -> dict | None:
    return _parse_op(raw, snapshot, state)


def editor_snapshot_list(snapshot: dict, keys: Iterable[str]) -> list:
    return _snapshot_list(snapshot, keys)
