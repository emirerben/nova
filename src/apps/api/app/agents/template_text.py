"""nova.compose.template_text — extract every on-screen text overlay.

Runs as a focused second pass on the same template video used by
`TemplateRecipeAgent`. The recipe agent is good at structural decomposition
(slots, transitions, style) but its `text_overlays` field is a side-concern in
a 60-line omnibus prompt — small text gets dropped, positions collapse to
top/center/bottom buckets, timing drifts.

This agent does one thing: enumerate every visible text overlay with a required
normalized bounding box, font color, and timing. Per-overlay completeness and
position fidelity are then measurable end-to-end via the eval harness against
OCR-derived ground truth.

Wired into `agentic_template_build` only — manual templates and music-job
templates are out of scope.
"""

from __future__ import annotations

import json
from typing import ClassVar

import structlog
from pydantic import BaseModel, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.template_text import (
    TemplateTextInput,
    TemplateTextOutput,
    TemplateTextOverlay,
)
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()


__all__ = [
    "TemplateTextAgent",
    "TemplateTextInput",
    "TemplateTextOutput",
    "TemplateTextOverlay",
]


class TemplateTextAgent(Agent[TemplateTextInput, TemplateTextOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.template_text",
        prompt_id="extract_template_text",
        prompt_version="2026-05-17",
        model="gemini-2.5-flash",
        # Same per-token rates as template_recipe; this is a second focused
        # pass on the same uploaded video, so input tokens are dominated by
        # the prompt text + video bytes (cached by Gemini's File API).
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        # Text extraction is more output-heavy than recipe extraction — a
        # template with 15 overlays produces ~3KB of JSON. Bump the implicit
        # max_output cap by setting timeout higher; the real cap is set by
        # the client. Five attempts is plenty.
        timeout_s=60.0,
    )
    Input = TemplateTextInput
    Output = TemplateTextOutput

    def media_uri(self, input: TemplateTextInput) -> str | None:  # noqa: A002
        return input.file_uri

    def media_mime(self, input: TemplateTextInput) -> str:  # noqa: A002
        return input.file_mime or "video/mp4"

    def required_fields(self) -> list[str]:
        return ["overlays"]

    def render_prompt(self, input: TemplateTextInput) -> str:  # noqa: A002
        return load_prompt(
            "extract_template_text",
            slot_boundaries_context=_render_slot_boundaries(input.slot_boundaries_s),
        )

    def parse(self, raw_text: str, input: TemplateTextInput) -> TemplateTextOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"template_text: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("template_text: response is not a JSON object")
        raw_overlays = data.get("overlays", [])
        if not isinstance(raw_overlays, list):
            raise SchemaError("template_text: overlays is not a list")

        # Clamp slot_index to the number of slots the agent was told about.
        # Out-of-range slot_index is silently coerced to the nearest valid
        # value (1 or len(slot_boundaries)) and logged. We DON'T drop the
        # overlay — losing the text entirely is worse than putting it on a
        # neighboring slot, since the renderer's cross-slot merge can recover.
        max_slot = max(len(input.slot_boundaries_s), 1)
        validated: list[TemplateTextOverlay] = []
        dropped = 0
        for i, ov in enumerate(raw_overlays):
            if not isinstance(ov, dict):
                log.warning("template_text_overlay_not_dict", index=i)
                dropped += 1
                continue
            si = ov.get("slot_index")
            if isinstance(si, (int, float)):
                si_int = int(si)
                if si_int < 1:
                    log.warning(
                        "template_text_overlay_slot_index_clamped",
                        index=i,
                        original=si_int,
                        clamped_to=1,
                    )
                    ov["slot_index"] = 1
                elif si_int > max_slot:
                    log.warning(
                        "template_text_overlay_slot_index_clamped",
                        index=i,
                        original=si_int,
                        clamped_to=max_slot,
                    )
                    ov["slot_index"] = max_slot
            # start_s <= end_s — try a swap salvage like template_recipe does.
            try:
                s = float(ov.get("start_s", 0.0))
                e = float(ov.get("end_s", 0.0))
            except (TypeError, ValueError):
                log.warning(
                    "template_text_overlay_non_numeric_timing",
                    index=i,
                    sample_text=ov.get("sample_text"),
                )
                dropped += 1
                continue
            if s > e and e >= 0.0:
                log.warning(
                    "template_text_overlay_timing_salvaged",
                    index=i,
                    sample_text=ov.get("sample_text"),
                    original_start=s,
                    original_end=e,
                )
                ov["start_s"], ov["end_s"] = e, s
            try:
                validated.append(TemplateTextOverlay.model_validate(ov))
            except ValidationError as exc:
                log.warning(
                    "template_text_overlay_invalid",
                    index=i,
                    sample_text=ov.get("sample_text"),
                    errors=str(exc),
                )
                dropped += 1
                continue

        if dropped:
            log.info("template_text_overlays_dropped", count=dropped, kept=len(validated))

        try:
            return TemplateTextOutput(overlays=validated)
        except ValidationError as exc:
            raise SchemaError(f"template_text: output validation — {exc}") from exc


# ── Prompt helpers ────────────────────────────────────────────────────────────


def _render_slot_boundaries(boundaries: list[tuple[float, float]]) -> str:
    """Format slot boundaries for prompt injection.

    Empty list returns an empty string so the prompt's $slot_boundaries_context
    substitution becomes a no-op when the agent is invoked without recipe
    context (e.g. in unit tests).
    """
    if not boundaries:
        return ""
    lines = ["Slot boundaries (global timeline, seconds):"]
    for i, (s, e) in enumerate(boundaries, start=1):
        lines.append(f"  slot {i}: {s:.2f}s → {e:.2f}s")
    lines.append(
        "Attribute each overlay to the slot whose window contains its start_s. "
        "An overlay that straddles a slot boundary belongs to the slot it starts in."
    )
    return "\n".join(lines)
