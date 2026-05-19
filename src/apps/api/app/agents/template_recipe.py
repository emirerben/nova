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
_VALID_TRANSITION_TYPES = {
    "hard-cut",
    "match-cut",
    "whip-pan",
    "zoom-in",
    "dissolve",
    "curtain-close",
    "speed-ramp",
    "none",
}
_VALID_COLOR_HINTS = {"warm", "cool", "high-contrast", "desaturated", "vintage", "none"}
_VALID_SYNC_STYLES = {"cut-on-beat", "transition-on-beat", "energy-match", "freeform"}
_VALID_INTERSTITIAL_TYPES = {"curtain-close", "barn-door-open", "fade-black-hold", "flash-white"}


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
        prompt_version="2026-05-17",
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
        # Pass the input's detected black_segments so the guard can drop
        # interstitials Gemini hallucinated for shot boundaries where no
        # dark frames actually exist. See the 24ac3408 incident (2026-05-15):
        # Gemini emitted a fade-black-hold with hold_s=2.0 for a template
        # with black_segments=[]. Pre-guard, the renderer faithfully built
        # a 2-second fade-to-black in the middle of a continuous shot.
        interstitials = _validate_interstitials(
            data.get("interstitials", []) or [],
            int(data.get("shot_count", 0) or 0),
            black_segments=input.black_segments,
        )

        # ── Recipe-validation guards (D2) ───────────────────────
        # Merge duplicate adjacent-slot overlays so the same logical text
        # doesn't restart on every slot boundary. Belt-and-braces with the
        # render-time same-text merge in _collect_absolute_overlays: parsing
        # the dedup at write time also cleans the stored recipe so future
        # job-time logs are not flooded with merge events.
        _dedup_overlays_across_slots(slots)

        # F8 fix: enforce pct uniformity across the whole recipe. The
        # renderer dispatches per overlay AND per slot on pct presence,
        # so a recipe with pct on some slots/overlays but not others would
        # produce mixed-mode output (some scaled, some legacy seconds in
        # the same render). Make it all-or-nothing: if ANY pct field is
        # missing anywhere, strip pct from EVERYTHING so the legacy path
        # runs uniformly. Logged so the agent prompt gets feedback.
        _enforce_pct_uniformity(slots)

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
    """Normalize per-slot fields in place. Lifted verbatim from legacy code.

    Also validates the agentic pct schema additively: `target_duration_pct`
    on the slot and `start_pct` / `end_pct` on each overlay. These are
    optional — classic recipes never carry them; agentic recipes emit both
    seconds AND pct so the renderer can dispatch on `is_agentic` and still
    have a legacy fallback when older agentic recipes lack pct fields.
    """
    for slot in slots:
        # target_duration_pct — agentic-only, optional, must be in (0, 1]
        # when present. Dropped silently if invalid so the legacy
        # target_duration_s path remains usable.
        pct_raw = slot.get("target_duration_pct")
        if pct_raw is not None:
            try:
                pct_val = float(pct_raw)
                if 0.0 < pct_val <= 1.0:
                    slot["target_duration_pct"] = pct_val
                else:
                    log.warning(
                        "template_slot_target_pct_out_of_range",
                        slot_position=slot.get("position"),
                        pct=pct_val,
                    )
                    slot.pop("target_duration_pct", None)
            except (TypeError, ValueError):
                log.warning(
                    "template_slot_target_pct_not_numeric",
                    slot_position=slot.get("position"),
                    raw=pct_raw,
                )
                slot.pop("target_duration_pct", None)

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
            ov_text = ov.get("text") or ov.get("sample_text")
            # Drop NaN/inf timings outright. `start >= end` returns False for
            # NaN comparisons, so these would otherwise slip every downstream
            # gate and reach the renderer.
            if not (math.isfinite(start) and math.isfinite(end)):
                log.warning(
                    "template_overlay_non_finite_timing",
                    text=ov_text,
                    start_s=start,
                    end_s=end,
                )
                continue
            # Salvage inverted (start, end) tuples. Gemini occasionally returns
            # the timing pair swapped — observed in prod on template
            # fdaf3bbc-… where overlay "It's" came back as start=3.0 end=0.88.
            # Require a known positive slot duration so both values stay
            # bounded after the swap; anything weirder gets dropped below.
            if start > end and end >= 0.0 and slot_dur > 0 and start <= slot_dur:
                log.warning(
                    "template_overlay_timing_salvaged",
                    text=ov_text,
                    original_start=start,
                    original_end=end,
                )
                start, end = end, start
                ov["start_s"] = start
                ov["end_s"] = end
            if start >= end:
                log.warning(
                    "template_overlay_bad_timing",
                    text=ov_text,
                    start_s=start,
                    end_s=end,
                )
                continue
            if slot_dur > 0 and start >= slot_dur:
                log.warning(
                    "template_overlay_outside_slot",
                    text=ov_text,
                    start_s=start,
                    slot_dur=slot_dur,
                )
                continue

            # Agentic pct fields — additive, both required-together, range
            # [0, 1] with start < end. Dropped from the overlay (not the
            # whole overlay) if invalid; the seconds fields still let the
            # overlay render. Agentic templates also emit start_s/end_s so
            # this is purely about whether the pct precedence path is usable.
            start_pct_raw = ov.get("start_pct")
            end_pct_raw = ov.get("end_pct")
            if start_pct_raw is not None or end_pct_raw is not None:
                pct_ok = False
                if start_pct_raw is not None and end_pct_raw is not None:
                    try:
                        s_pct = float(start_pct_raw)
                        e_pct = float(end_pct_raw)
                        if 0.0 <= s_pct < e_pct <= 1.0:
                            ov["start_pct"] = s_pct
                            ov["end_pct"] = e_pct
                            pct_ok = True
                    except (TypeError, ValueError):
                        pass
                if not pct_ok:
                    log.warning(
                        "template_overlay_pct_invalid",
                        start_pct=start_pct_raw,
                        end_pct=end_pct_raw,
                    )
                    ov.pop("start_pct", None)
                    ov.pop("end_pct", None)
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
            ov["text_bbox"] = _validate_text_bbox(ov.get("text_bbox"), start, end)
            validated.append(ov)
        slot["text_overlays"] = validated


def _validate_text_bbox(
    raw: Any, overlay_start_s: float, overlay_end_s: float
) -> dict[str, float] | None:
    """Validate optional text_bbox on an overlay. Returns None for absent/invalid.

    Schema (all fields required when present):
      x_norm, y_norm: center coords in [0, 1]
      w_norm, h_norm: dims in (0, 1]
      sample_frame_t: seconds, must satisfy overlay_start_s <= t <= overlay_end_s

    Invalid bboxes are dropped (logged) so the overlay itself remains usable.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        log.warning("template_overlay_bbox_not_dict", raw=raw)
        return None
    try:
        x = float(raw.get("x_norm"))
        y = float(raw.get("y_norm"))
        w = float(raw.get("w_norm"))
        h = float(raw.get("h_norm"))
        t = float(raw.get("sample_frame_t"))
    except (TypeError, ValueError):
        log.warning("template_overlay_bbox_non_numeric", raw=raw)
        return None
    for name, v in (("x_norm", x), ("y_norm", y), ("w_norm", w), ("h_norm", h)):
        if math.isnan(v) or math.isinf(v):
            log.warning("template_overlay_bbox_nan", field=name)
            return None
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        log.warning("template_overlay_bbox_center_out_of_range", x=x, y=y)
        return None
    if not (0.0 < w <= 1.0 and 0.0 < h <= 1.0):
        log.warning("template_overlay_bbox_zero_or_oversize_area", w=w, h=h)
        return None
    if (x + w / 2.0) > 1.0 or (x - w / 2.0) < 0.0:
        log.warning("template_overlay_bbox_horizontal_overflow", x=x, w=w)
        return None
    if (y + h / 2.0) > 1.0 or (y - h / 2.0) < 0.0:
        log.warning("template_overlay_bbox_vertical_overflow", y=y, h=h)
        return None
    if t < overlay_start_s or t > overlay_end_s:
        log.warning(
            "template_overlay_bbox_sample_frame_outside_window",
            sample_frame_t=t,
            start_s=overlay_start_s,
            end_s=overlay_end_s,
        )
        return None
    return {"x_norm": x, "y_norm": y, "w_norm": w, "h_norm": h, "sample_frame_t": t}


def _validate_interstitials(
    raw: list[Any],
    shot_count: int,
    *,
    black_segments: list[BlackSegment] | None = None,
) -> list[dict[str, Any]]:
    """Validate + normalize interstitial entries. Drops invalid ones silently.

    When `black_segments` is provided (D2 grounding guard), interstitials
    that claim more than _MIN_GROUNDED_HOLD_S of black/white hold but have
    no corresponding detected dark segment are dropped. This protects
    against Gemini hallucinating fade-to-black transitions where none
    exist in the source video, which would otherwise be rendered faithfully
    as a literal blackout in the output (the 24ac3408 incident).

    Interstitials with hold_s below the threshold pass through unfiltered —
    those are rhythmic black-frame emphasis the agent may legitimately add
    even without a measurable dark segment.
    """
    if not isinstance(raw, list):
        return []

    has_any_black = bool(black_segments) and len(black_segments) > 0
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

        # D2 grounding guard — only enforced when the caller passed
        # black_segments (i.e. we have FFmpeg-detected evidence to check
        # against). If the input list is None, we behave as before.
        #
        # F4 fix: the guard only applies to interstitial types that ARE
        # detectable in black_segments. flash-white is a WHITE frame, and
        # barn-door-open is a reveal animation — neither would appear in
        # black-segment detection. Without this scope, those legitimate
        # interstitials with hold_s >= 0.5s were being silently dropped.
        if (
            black_segments is not None
            and itype in _BLACK_FRAME_INTERSTITIAL_TYPES
            and hold_s >= _MIN_GROUNDED_HOLD_S
            and not has_any_black
        ):
            log.warning(
                "interstitial_hallucinated_dropped",
                type=itype,
                after_slot=after_slot,
                hold_s=hold_s,
                reason="no detected black segment supports this transition",
            )
            continue

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


def _enforce_pct_uniformity(slots: list[dict[str, Any]]) -> None:
    """Strip pct fields from every slot+overlay if uniformity is broken.

    Pct presence is all-or-nothing per recipe. The renderer dispatches on
    `is_agentic + pct present` per slot and per overlay; a recipe with
    pct on some slots and not others would render some slots scaled and
    others at frozen seconds. That's worse than either pure mode because
    the operator cannot predict slot durations.

    Strategy: count what's there, then either keep all or strip all.
    Slot-level pct and overlay-level pct are evaluated together — either
    every slot has target_duration_pct AND every overlay has both
    start_pct and end_pct, or nothing has pct.
    """
    if not slots:
        return

    slot_count = len(slots)
    slots_with_pct = sum(1 for s in slots if s.get("target_duration_pct") is not None)

    overlay_count = 0
    overlays_with_pct = 0
    for slot in slots:
        for ov in slot.get("text_overlays", []):
            overlay_count += 1
            if ov.get("start_pct") is not None and ov.get("end_pct") is not None:
                overlays_with_pct += 1

    fully_pct = slots_with_pct == slot_count and (
        overlay_count == 0 or overlays_with_pct == overlay_count
    )
    fully_legacy = slots_with_pct == 0 and overlays_with_pct == 0

    if fully_pct or fully_legacy:
        return

    # Partial — strip pct everywhere so the legacy seconds path renders
    # the entire recipe uniformly.
    log.warning(
        "template_pct_partial_stripped",
        slots_with_pct=slots_with_pct,
        slot_count=slot_count,
        overlays_with_pct=overlays_with_pct,
        overlay_count=overlay_count,
        reason="pct fields present on some slots/overlays but not all — "
        "stripped for uniform legacy rendering",
    )
    for slot in slots:
        slot.pop("target_duration_pct", None)
        for ov in slot.get("text_overlays", []):
            ov.pop("start_pct", None)
            ov.pop("end_pct", None)


# Below this hold duration, we don't require a matching black_segment.
# A 2-4 frame (~0.1s) black emphasis is a stylistic choice the editor may
# add without it being a true fade. A 0.5s+ hold is structural and must be
# anchored to actual dark frames in the source video.
_MIN_GROUNDED_HOLD_S = 0.5

# Only these interstitial types produce a black hold detectable by FFmpeg
# black-segment detection. The other types in _VALID_INTERSTITIAL_TYPES
# (flash-white, barn-door-open) are white or animated reveals and cannot
# be cross-checked against black_segments — so the grounding guard skips
# them entirely.
_BLACK_FRAME_INTERSTITIAL_TYPES = {"fade-black-hold", "curtain-close"}


def _dedup_overlays_across_slots(slots: list[dict[str, Any]]) -> None:
    """Merge duplicate same-text overlays that span adjacent slots.

    Catches the failure mode where Gemini fragments a logical overlay
    ("being rich in life" reading 5s→24s of a continuous shot) across
    two slot boundaries it shouldn't have introduced. The 24ac3408
    16:35 recipe is the canonical case: slot 1 ends at 15s with text X,
    slot 2 starts at 0s with text X — the renderer plays the same text
    on both sides of an interstitial, producing a visual "restart".

    Pairwise pass: for each (slot_i, slot_i+1), find overlays on slot_i+1
    whose normalized sample_text and position match an overlay on slot_i.
    EXTEND slot_i's overlay end_s to cover the duration the duplicate
    occupied on slot_i+1, then drop the duplicate from slot_i+1. This
    preserves designer intent (the text was meant to stay visible across
    the boundary) and mirrors the renderer's existing same-text merge in
    `_collect_absolute_overlays`. Both layers run for defence in depth.

    Operates in place on `slots`. Logged so prompt regressions surface
    in agent_evals.
    """
    if len(slots) < 2:
        return

    def _norm(s: Any) -> str:
        return str(s or "").strip().lower()

    for i in range(len(slots) - 1):
        slot_a = slots[i]
        slot_b = slots[i + 1]
        overlays_a = slot_a.get("text_overlays", [])
        overlays_b = slot_b.get("text_overlays", [])
        if not overlays_a or not overlays_b:
            continue
        slot_a_dur = float(slot_a.get("target_duration_s", 0.0) or 0.0)
        # Map (text, position) → reference to the slot_a overlay so we can
        # extend its end_s when a duplicate is found on slot_b.
        a_by_key: dict[tuple[str, str], dict[str, Any]] = {
            (_norm(ov.get("sample_text")), str(ov.get("position", "center"))): ov
            for ov in overlays_a
            if _norm(ov.get("sample_text"))
        }
        if not a_by_key:
            continue
        kept_b: list[dict[str, Any]] = []
        for ov in overlays_b:
            key = (_norm(ov.get("sample_text")), str(ov.get("position", "center")))
            ref_a = a_by_key.get(key)
            if ref_a is None:
                kept_b.append(ov)
                continue
            # Merge: extend slot_a's overlay so the cross-slot duration of
            # this text reads as one continuous overlay. start stays put;
            # end extends by however long the duplicate occupied on slot_b.
            b_extra = max(
                0.0, float(ov.get("end_s", 0.0) or 0.0) - float(ov.get("start_s", 0.0) or 0.0)
            )
            old_end_a = float(ref_a.get("end_s", slot_a_dur) or slot_a_dur)
            new_end_a = old_end_a + b_extra
            if new_end_a > old_end_a:
                ref_a["end_s"] = new_end_a
            # If the recipe carries pct fields too (agentic), extend the
            # pct in the same proportion. Without this, the seconds and
            # pct end values would diverge and the renderer's dispatch
            # would produce different windows depending on is_agentic.
            if "end_pct" in ref_a and slot_a_dur > 0:
                try:
                    new_end_pct = min(1.0, float(ref_a["end_pct"]) + b_extra / slot_a_dur)
                    ref_a["end_pct"] = new_end_pct
                except (TypeError, ValueError):
                    pass
            log.warning(
                "template_overlay_dedup_merged",
                slot_position=slot_b.get("position"),
                sample_text=ov.get("sample_text"),
                position=ov.get("position"),
                merged_into_slot=slot_a.get("position"),
                extra_s=round(b_extra, 3),
                new_end_s_a=round(new_end_a, 3),
                reason="same text+position on preceding slot, end extended",
            )
        slot_b["text_overlays"] = kept_b
