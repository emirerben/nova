"""Locate visible actions within a bounded source sample, without choosing an edit."""

from __future__ import annotations

import json
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents.clip_question import ClipQuestionInput
from app.pipeline.prompt_loader import load_prompt


class LocatedSourceWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_s - self.start_s < 0.3:
            raise ValueError("A located action must occupy at least 0.3 seconds")
        return self


class SourceMomentLocationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["found", "absent", "uncertain"]
    matches: list[LocatedSourceWindow] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def consistent(self):
        if bool(self.matches) != (self.status == "found"):
            raise ValueError("Only a found result may contain located matches")
        return self


class SourceMomentLocationAgent(Agent[ClipQuestionInput, SourceMomentLocationOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.video.source_moment_location",
        prompt_id="source_moment_location",
        prompt_version="2026-10-30-bounded-source-time-v2",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=128,
        max_attempts=1,
        timeout_s=12.0,
        sensitive_io=True,
        enable_clarification_retries=False,
    )
    Input = ClipQuestionInput
    Output = SourceMomentLocationOutput

    def media_uri(self, input: ClipQuestionInput) -> str | None:  # noqa: A002
        return input.file_uri

    def media_mime(self, input: ClipQuestionInput) -> str:  # noqa: A002
        return input.file_mime

    def required_fields(self) -> list[str]:
        return []

    def render_prompt(self, input: ClipQuestionInput) -> str:  # noqa: A002
        return load_prompt(
            "source_moment_location", question=json.dumps(input.question, ensure_ascii=False)
        )

    def parse(self, raw_text: str, input: ClipQuestionInput) -> SourceMomentLocationOutput:  # noqa: A002
        try:
            return SourceMomentLocationOutput.model_validate_json(raw_text)
        except (ValidationError, ValueError) as exc:
            raise SchemaError("Invalid bounded source-time location output") from exc
