"""Choose transcript-grounded montage and text-card opportunities."""

from __future__ import annotations

import json
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, field_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt


class VisualTreatmentAsset(BaseModel):
    asset_id: str
    kind: Literal["image", "video"]
    user_context: str = ""
    subject: str = ""
    description: str = ""
    on_screen_text: str = ""


class VisualTreatmentPlannerInput(BaseModel):
    # Bounds scale with the five-minute source ceiling. Whisper can emit far
    # more than one token per spoken word, and energetic music can exceed one
    # beat per second, so the old short-form caps rejected valid long inputs.
    words: list[dict] = Field(default_factory=list, max_length=6000)
    assets: list[VisualTreatmentAsset] = Field(default_factory=list, max_length=20)
    # Generative inputs may be the full creator recording, not only a sub-60s
    # short. Five minutes matches Nova's documented source-video ceiling.
    duration_s: float = Field(gt=0.0, le=300.0)
    beat_timestamps_s: list[float] = Field(default_factory=list, max_length=3000)
    clip_plan: list[dict] = Field(default_factory=list, max_length=600)
    visual_signals: dict = Field(default_factory=dict)
    recent_treatments: list[dict] = Field(default_factory=list, max_length=20)


class RawVisualTreatment(BaseModel):
    kind: Literal["montage", "text_card", "footage"]
    purpose: Literal[
        "hook",
        "context",
        "key_argument",
        "example",
        "quote",
        "statistic",
        "transition",
        "section_item",
        "conclusion",
        "cta",
    ]
    start_s: float = Field(ge=0.0)
    end_s: float = Field(gt=0.0)
    asset_ids: list[str] = Field(default_factory=list, max_length=10)
    text: str | None = Field(default=None, max_length=500)
    rationale: str = Field(default="", max_length=300)
    confidence: Literal["high", "medium", "low"] = "medium"

    @field_validator("text")
    @classmethod
    def _text(cls, value: str | None) -> str | None:
        return (value or "").strip()[:500] or None


class VisualTreatmentPlannerOutput(BaseModel):
    treatments: list[RawVisualTreatment] = Field(default_factory=list, max_length=12)


class VisualTreatmentPlannerAgent(Agent[VisualTreatmentPlannerInput, VisualTreatmentPlannerOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.visual_treatment_planner",
        prompt_id="visual_treatment_planner",
        prompt_version="2026-07-24.1",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=512,
        enable_json_repair=True,
    )
    Input = VisualTreatmentPlannerInput
    Output = VisualTreatmentPlannerOutput

    def required_fields(self) -> list[str]:
        return ["treatments"]

    def render_prompt(self, input: VisualTreatmentPlannerInput) -> str:  # noqa: A002
        return load_prompt(
            "visual_treatment_planner",
            words_json=json.dumps(input.words, ensure_ascii=False),
            assets_json=json.dumps(
                [asset.model_dump() for asset in input.assets], ensure_ascii=False
            ),
            duration_s=f"{input.duration_s:.2f}",
            beats_json=json.dumps(input.beat_timestamps_s),
            clip_plan_json=json.dumps(input.clip_plan, ensure_ascii=False),
            signals_json=json.dumps(input.visual_signals, ensure_ascii=False),
            recent_json=json.dumps(input.recent_treatments, ensure_ascii=False),
        )

    def parse(
        self,
        raw_text: str,
        input: VisualTreatmentPlannerInput,  # noqa: A002, ARG002
    ) -> VisualTreatmentPlannerOutput:
        try:
            data = json.loads(raw_text)
            return VisualTreatmentPlannerOutput.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            raise SchemaError(f"visual_treatment_planner: invalid output — {exc}") from exc
