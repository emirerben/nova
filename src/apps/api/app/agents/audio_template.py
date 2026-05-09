"""nova.audio.template_recipe — beat/slot recipe extraction from a music track.

Replaces `app.pipeline.agents.gemini_analyzer.analyze_audio_template`. Mirrors
the existing return dict shape exactly so callers translate transparently.
"""

from __future__ import annotations

import json
import math
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.agents.template_recipe import (
    _VALID_COLOR_HINTS,
    _VALID_SYNC_STYLES,
    _validate_interstitials,
    _validate_slots,
)
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()


class AudioTemplateInput(BaseModel):
    file_uri: str
    file_mime: str = "audio/mp4"
    beat_timestamps_s: list[float]
    best_start_s: float = 0.0
    best_end_s: float = 0.0
    duration_s: float = 0.0


class AudioTemplateOutput(BaseModel):
    """Output mirrors the dict returned by `analyze_audio_template` field-for-field."""

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
    sync_style: str = "cut-on-beat"
    interstitials: list[dict[str, Any]] = Field(default_factory=list)
    subject_niche: str = ""
    has_talking_head: bool = False  # always False for audio-only
    has_voiceover: bool = False     # always False for audio-only
    has_permanent_letterbox: bool = False  # always False for audio-only

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class AudioTemplateAgent(Agent[AudioTemplateInput, AudioTemplateOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.audio.template_recipe",
        prompt_id="analyze_audio_template",
        prompt_version="2026-05-09",
        model="gemini-2.5-flash",
        # Audio analysis is more transient-prone in practice (large file uploads);
        # legacy used 3 attempts × 15s. The runtime default of 5 × [3,9,27,60] is
        # more generous and matches the pattern used by other agents.
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = AudioTemplateInput
    Output = AudioTemplateOutput

    def media_uri(self, input: AudioTemplateInput) -> str | None:  # noqa: A002
        return input.file_uri

    def media_mime(self, input: AudioTemplateInput) -> str:  # noqa: A002
        return input.file_mime or "audio/mp4"

    def required_fields(self) -> list[str]:
        return ["slots", "shot_count", "total_duration_s"]

    def render_prompt(self, input: AudioTemplateInput) -> str:  # noqa: A002
        # Slice beats to the best section.
        section_beats = [
            b for b in input.beat_timestamps_s
            if input.best_start_s <= b <= input.best_end_s
        ]
        section_duration = input.best_end_s - input.best_start_s

        schema = load_prompt("analyze_template_schema")
        return load_prompt(
            "analyze_audio_template",
            beat_timestamps_s=str([round(b, 2) for b in section_beats]),
            best_start_s=str(round(input.best_start_s, 2)),
            best_end_s=str(round(input.best_end_s, 2)),
            duration_s=str(round(section_duration, 2)),
            schema=schema,
        )

    def parse(
        self, raw_text: str, input: AudioTemplateInput
    ) -> AudioTemplateOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"audio_template: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("audio_template: response is not a JSON object")

        slots = data.get("slots", [])
        if not isinstance(slots, list):
            raise SchemaError("audio_template: slots is not a list")

        for i, slot in enumerate(slots):
            if not isinstance(slot, dict):
                raise SchemaError(f"audio_template: slot {i + 1} not an object")
            dur = slot.get("target_duration_s")
            try:
                dur_val = float(dur) if dur is not None else 0.0
            except (TypeError, ValueError):
                raise RefusalError(
                    f"Slot {i + 1} has non-numeric target_duration_s: {dur}"
                ) from None
            if dur_val <= 0 or math.isnan(dur_val) or math.isinf(dur_val):
                raise RefusalError(f"Slot {i + 1} missing or invalid target_duration_s")
            # audio_template legacy was lenient: defaults missing slot_type to "broll"
            if not slot.get("slot_type"):
                slot["slot_type"] = "broll"

        global_color_grade = str(data.get("color_grade", "none") or "none")
        if global_color_grade not in _VALID_COLOR_HINTS:
            global_color_grade = "none"
        _validate_slots(slots, global_color_grade)

        interstitials = _validate_interstitials(
            data.get("interstitials", []) or [], int(data.get("shot_count", 0) or 0)
        )

        sync_style = str(data.get("sync_style", "cut-on-beat") or "cut-on-beat")
        if sync_style not in _VALID_SYNC_STYLES:
            sync_style = "cut-on-beat"

        section_beats = [
            b for b in input.beat_timestamps_s
            if input.best_start_s <= b <= input.best_end_s
        ]
        section_duration = input.best_end_s - input.best_start_s

        try:
            return AudioTemplateOutput(
                shot_count=int(data.get("shot_count", len(slots)) or len(slots)),
                total_duration_s=float(
                    data.get("total_duration_s", section_duration) or section_duration
                ),
                hook_duration_s=float(data.get("hook_duration_s", 0.0) or 0.0),
                slots=slots,
                copy_tone=str(data.get("copy_tone", "energetic") or "energetic"),
                caption_style=str(data.get("caption_style", "") or ""),
                # Legacy returns beats relative to best_start (offset by -best_start)
                beat_timestamps_s=[round(b - input.best_start_s, 3) for b in section_beats],
                creative_direction=str(data.get("creative_direction", "") or ""),
                transition_style=str(data.get("transition_style", "") or ""),
                color_grade=global_color_grade,
                pacing_style=str(data.get("pacing_style", "") or ""),
                sync_style=sync_style,
                interstitials=interstitials,
                subject_niche=str(data.get("subject_niche", "") or ""),
                has_talking_head=False,
                has_voiceover=False,
                has_permanent_letterbox=False,
            )
        except ValidationError as exc:
            raise SchemaError(f"audio_template: output validation — {exc}") from exc
