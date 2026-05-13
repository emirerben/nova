"""nova.compose.template_recipe — extract structural recipe from a template video.

Replaces `app.pipeline.agents.gemini_analyzer.analyze_template` (single-pass and
two-pass-Pass-2). The legacy function is kept as a thin shim that handles two-pass
orchestration (Pass 1 creative_direction → Pass 2 structural extraction via this
agent).

Schemas mirror the existing `TemplateRecipe` dataclass exactly. Slot + interstitial
validation logic is lifted verbatim from the legacy code — same constants, same
graceful-degradation semantics.
"""

from __future__ import annotations

import json
import math
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import (
    Agent,
    AgentSpec,
    RefusalError,
    SchemaError,
)
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()


# ── Validation constants (lifted from gemini_analyzer.py) ─────────────────────

_VALID_OVERLAY_ROLES = {"hook", "reaction", "cta", "label"}
_VALID_OVERLAY_EFFECTS = {
    "pop-in",
    "fade-in",
    "scale-up",
    "none",
    "font-cycle",
    "typewriter",
    "glitch",
    "bounce",
    "slide-in",
    "slide-up",
    "static",
}
_VALID_OVERLAY_POSITIONS = {"center", "top", "bottom"}
_VALID_OVERLAY_FONT_SIZES = {"small", "medium", "large", "jumbo"}
_VALID_CAMERA_MOVEMENTS = {
    "static",
    "pan-left",
    "pan-right",
    "tilt-up",
    "tilt-down",
    "zoom-in",
    "zoom-out",
    "handheld",
    "tracking",
}
_VALID_TRANSITION_TYPES = {"hard-cut", "whip-pan", "zoom-in", "dissolve", "curtain-close", "none"}
_VALID_COLOR_HINTS = {"warm", "cool", "high-contrast", "desaturated", "vintage", "none"}
_VALID_SYNC_STYLES = {"cut-on-beat", "transition-on-beat", "energy-match", "freeform"}
_VALID_INTERSTITIAL_TYPES = {"curtain-close", "fade-black-hold", "flash-white"}


# ── Schemas ───────────────────────────────────────────────────────────────────


class BlackSegment(BaseModel):
    start_s: float
    end_s: float
    duration_s: float
    likely_type: str | None = None  # "curtain-close" | "fade-black-hold" | None


class TemplateRecipeInput(BaseModel):
    file_uri: str
    file_mime: str = "video/mp4"
    analysis_mode: str = "single"  # "single" | "two_pass_part2"
    creative_direction: str = ""  # populated by caller in two-pass mode
    black_segments: list[BlackSegment] = Field(default_factory=list)


class TemplateRecipeOutput(BaseModel):
    """Mirrors the legacy `TemplateRecipe` dataclass field-for-field.

    The legacy callers do `gemini_analyzer.analyze_template(...) -> TemplateRecipe`;
    the shim converts this Pydantic Output into that dataclass at the boundary.
    """

    shot_count: int
    total_duration_s: float
    hook_duration_s: float
    slots: list[dict[str, Any]]
    copy_tone: str
    caption_style: str
    beat_timestamps_s: list[float] = Field(default_factory=list)
    creative_direction: str = ""
    transition_style: str = ""
    color_grade: str = "none"
    pacing_style: str = ""
    sync_style: str = "freeform"
    interstitials: list[dict[str, Any]] = Field(default_factory=list)
    subject_niche: str = ""
    has_talking_head: bool = False
    has_voiceover: bool = False
    has_permanent_letterbox: bool = False


# ── Agent ─────────────────────────────────────────────────────────────────────


class TemplateRecipeAgent(Agent[TemplateRecipeInput, TemplateRecipeOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.template_recipe",
        prompt_id="analyze_template_single",  # selected dynamically in render_prompt
        prompt_version="2026-05-14",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = TemplateRecipeInput
    Output = TemplateRecipeOutput

    def media_uri(self, input: TemplateRecipeInput) -> str | None:  # noqa: A002
        return input.file_uri

    def media_mime(self, input: TemplateRecipeInput) -> str:  # noqa: A002
        return input.file_mime or "video/mp4"

    def required_fields(self) -> list[str]:
        return ["shot_count", "slots"]

    # ── Prompt ────────────────────────────────────────────────────

    def render_prompt(self, input: TemplateRecipeInput) -> str:  # noqa: A002
        schema = load_prompt("analyze_template_schema")
        black_segments_context = self._build_black_segments_context(input.black_segments)

        if input.analysis_mode == "two_pass_part2" and input.creative_direction:
            return load_prompt(
                "analyze_template_pass2",
                creative_direction=input.creative_direction,
                schema=schema,
                black_segments_context=black_segments_context,
            )
        return load_prompt(
            "analyze_template_single",
            schema=schema,
            black_segments_context=black_segments_context,
        )

    @staticmethod
    def _build_black_segments_context(segments: list[BlackSegment]) -> str:
        if not segments:
            return ""
        lines = ["Black segments detected by frame analysis:"]
        has_classified = False
        for seg in segments:
            line = f"  - {seg.start_s:.2f}s to {seg.end_s:.2f}s (duration: {seg.duration_s:.2f}s)"
            if seg.likely_type == "curtain-close":
                line += " — CURTAIN-CLOSE detected (black bars closing from top/bottom)"
                has_classified = True
            elif seg.likely_type == "fade-black-hold":
                line += " — FADE-TO-BLACK detected (uniform darkening)"
                has_classified = True
            lines.append(line)
        if has_classified:
            lines.append(
                "Use the transition type classifications above when populating "
                "each interstitial's type field. Segments marked CURTAIN-CLOSE "
                'must use type "curtain-close" in the interstitials array.'
            )
        else:
            lines.append(
                "These may indicate curtain-close, fade-to-black, or other "
                "transitions with a black hold between clips."
            )
        return "\n".join(lines)

    # ── Parse ─────────────────────────────────────────────────────

    def parse(self, raw_text: str, input: TemplateRecipeInput) -> TemplateRecipeOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"template_recipe: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("template_recipe: response is not a JSON object")

        # ── Per-slot duration sanity (refusals on bad/missing values) ─
        slots = data.get("slots", [])
        if not isinstance(slots, list):
            raise SchemaError("template_recipe: slots is not a list")

        for i, slot in enumerate(slots):
            if not isinstance(slot, dict):
                raise SchemaError(f"template_recipe: slot {i + 1} is not an object")
            duration = slot.get("target_duration_s")
            try:
                dur_val = float(duration) if duration is not None else 0.0
            except (TypeError, ValueError):
                raise RefusalError(
                    f"Slot {i + 1} has non-numeric target_duration_s: {duration}"
                ) from None
            if dur_val <= 0 or math.isnan(dur_val) or math.isinf(dur_val):
                raise RefusalError(f"Slot {i + 1} missing or invalid target_duration_s")
            if not slot.get("slot_type"):
                raise RefusalError(f"Slot {i + 1} missing slot_type")

        total_duration_s = float(data.get("total_duration_s", 0.0) or 0.0)
        slot_duration_sum = sum(float(s.get("target_duration_s", 0.0) or 0.0) for s in slots)
        if total_duration_s > 0 and abs(slot_duration_sum - total_duration_s) > 5.0:
            log.warning(
                "template_slot_duration_mismatch",
                slot_sum=round(slot_duration_sum, 1),
                total_duration_s=round(total_duration_s, 1),
                delta=round(abs(slot_duration_sum - total_duration_s), 1),
            )

        # ── Color grade ───────────────────────────────────────────
        global_color_grade = str(data.get("color_grade", "none") or "none")
        if global_color_grade not in _VALID_COLOR_HINTS:
            global_color_grade = "none"

        _validate_slots(slots, global_color_grade)

        # ── Beats ────────────────────────────────────────────────
        try:
            beat_timestamps_s = sorted(float(t) for t in (data.get("beat_timestamps_s", []) or []))
        except (TypeError, ValueError):
            beat_timestamps_s = []

        # ── Creative direction (Pass-1 input wins; else use JSON field) ─
        creative_direction = input.creative_direction
        if not creative_direction:
            creative_direction = str(data.get("creative_direction", "") or "")

        # ── Sync style ───────────────────────────────────────────
        sync_style = str(data.get("sync_style", "freeform") or "freeform")
        if sync_style not in _VALID_SYNC_STYLES:
            sync_style = "freeform"

        # ── Interstitials ────────────────────────────────────────
        interstitials = _validate_interstitials(
            data.get("interstitials", []) or [], int(data.get("shot_count", 0) or 0)
        )

        try:
            return TemplateRecipeOutput(
                shot_count=int(data.get("shot_count", 0) or 0),
                total_duration_s=total_duration_s,
                hook_duration_s=float(data.get("hook_duration_s", 0.0) or 0.0),
                slots=slots,
                copy_tone=str(data.get("copy_tone", "casual") or "casual"),
                caption_style=str(data.get("caption_style", "") or ""),
                beat_timestamps_s=beat_timestamps_s,
                creative_direction=creative_direction,
                transition_style=str(data.get("transition_style", "") or ""),
                color_grade=global_color_grade,
                pacing_style=str(data.get("pacing_style", "") or ""),
                sync_style=sync_style,
                interstitials=interstitials,
                subject_niche=str(data.get("subject_niche", "") or ""),
                has_talking_head=bool(data.get("has_talking_head", False)),
                has_voiceover=bool(data.get("has_voiceover", False)),
                has_permanent_letterbox=bool(data.get("has_permanent_letterbox", False)),
            )
        except ValidationError as exc:
            raise SchemaError(f"template_recipe: output validation — {exc}") from exc


# ── Slot + interstitial validators (lifted from gemini_analyzer.py) ──────────


def _validate_slots(slots: list[dict[str, Any]], global_color_grade: str) -> None:
    """Normalize per-slot fields in place. Lifted verbatim from legacy code."""
    for slot in slots:
        # transition_in
        transition_in = slot.get("transition_in", "none")
        slot["transition_in"] = (
            transition_in if transition_in in _VALID_TRANSITION_TYPES else "none"
        )

        # color_hint (inherits global if missing/invalid)
        color_hint = slot.get("color_hint")
        if not color_hint or color_hint not in _VALID_COLOR_HINTS:
            slot["color_hint"] = global_color_grade

        # speed_factor (clamped)
        try:
            speed_factor = float(slot.get("speed_factor", 1.0) or 1.0)
        except (TypeError, ValueError):
            speed_factor = 1.0
        slot["speed_factor"] = max(0.25, min(4.0, speed_factor))

        # energy default
        if "energy" not in slot:
            slot["energy"] = 5.0

        # camera_movement
        camera_movement = slot.get("camera_movement", "static")
        slot["camera_movement"] = (
            camera_movement if camera_movement in _VALID_CAMERA_MOVEMENTS else "static"
        )

        # text_overlays
        raw_overlays = slot.get("text_overlays", [])
        if not isinstance(raw_overlays, list):
            slot["text_overlays"] = []
            continue
        validated: list[dict[str, Any]] = []
        slot_dur = float(slot.get("target_duration_s", 0.0) or 0.0)
        for ov in raw_overlays:
            if not isinstance(ov, dict):
                continue
            role = ov.get("role", "")
            if role not in _VALID_OVERLAY_ROLES:
                log.warning("template_overlay_invalid_role", role=role)
                continue
            try:
                start = float(ov.get("start_s", 0.0) or 0.0)
                end = float(ov.get("end_s", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if start >= end:
                log.warning("template_overlay_bad_timing", start_s=start, end_s=end)
                continue
            if slot_dur > 0 and start >= slot_dur:
                log.warning("template_overlay_outside_slot", start_s=start, slot_dur=slot_dur)
                continue
            ov["effect"] = (
                ov.get("effect", "none") if ov.get("effect") in _VALID_OVERLAY_EFFECTS else "none"
            )
            ov["position"] = (
                ov.get("position", "center")
                if ov.get("position") in _VALID_OVERLAY_POSITIONS
                else "center"
            )
            ov["font_size_hint"] = (
                ov.get("font_size_hint", "large")
                if ov.get("font_size_hint") in _VALID_OVERLAY_FONT_SIZES
                else "large"
            )
            ov.setdefault("has_darkening", False)
            ov.setdefault("has_narrowing", False)
            if not ov.get("sample_text") and ov.get("text"):
                ov["sample_text"] = ov["text"]
            ov.setdefault("sample_text", "")
            validated.append(ov)
        slot["text_overlays"] = validated


def _validate_interstitials(raw: list[Any], shot_count: int) -> list[dict[str, Any]]:
    """Validate + normalize interstitial entries. Drops invalid ones silently."""
    if not isinstance(raw, list):
        return []
    validated: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "")
        if itype not in _VALID_INTERSTITIAL_TYPES:
            log.warning("interstitial_invalid_type", type=itype)
            continue
        try:
            after_slot = int(item.get("after_slot", 0))
        except (TypeError, ValueError):
            continue
        if after_slot < 1 or after_slot > shot_count:
            log.warning("interstitial_invalid_slot", after_slot=after_slot)
            continue
        try:
            animate_s = max(0.0, min(2.0, float(item.get("animate_s", 0.5))))
            hold_s = max(0.1, min(3.0, float(item.get("hold_s", 1.0))))
        except (TypeError, ValueError):
            animate_s = 0.5
            hold_s = 1.0
        hold_color = item.get("hold_color", "#000000")
        if hold_color not in ("#000000", "#FFFFFF"):
            hold_color = "#000000"
        validated.append(
            {
                "after_slot": after_slot,
                "type": itype,
                "animate_s": animate_s,
                "hold_s": hold_s,
                "hold_color": hold_color,
            }
        )
    if validated:
        log.info("interstitials_validated", count=len(validated))
    return validated
