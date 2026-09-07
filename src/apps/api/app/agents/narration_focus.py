"""Bounded visual evidence producer for editable participant labels.

This agent is intentionally separate from narration semantics.  It receives a
server-pinned contact sheet for one source asset and one source-time window,
then returns only a typed subject-count claim plus the frame IDs it used.  It
never names, matches, or tracks a person across assets.
"""

from __future__ import annotations

import json
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agents._model_client import ModelClient
from app.agents._runtime import Agent, AgentSpec, RunContext, SchemaError
from app.pipeline.prompt_loader import load_prompt


class FocusFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str = Field(min_length=1, max_length=120)
    source_time_s: float = Field(ge=0)


class NarrationFocusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=240)
    source_instance_id: str = Field(default="", max_length=240)
    frame_contact_sheet_uri: str = Field(min_length=1, max_length=2000)
    frame_samples: list[FocusFrame] = Field(min_length=1, max_length=6)
    source_window_start_s: float = Field(ge=0)
    source_window_end_s: float = Field(gt=0)

    @model_validator(mode="after")
    def valid_window(self) -> NarrationFocusInput:
        if self.source_window_end_s <= self.source_window_start_s:
            raise ValueError("source window must have positive duration")
        if any(
            sample.source_time_s < self.source_window_start_s
            or sample.source_time_s > self.source_window_end_s
            for sample in self.frame_samples
        ):
            raise ValueError("frame sample is outside the pinned source window")
        return self


class NarrationFocusOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    focus: Literal["single_subject", "group", "unknown"] = "unknown"
    primary_subject_count: int | None = Field(default=None, ge=0, le=32)
    visible_subject_count: int | None = Field(default=None, ge=0, le=32)
    evidence_frame_ids: list[str] = Field(default_factory=list, max_length=6)
    evidence: str = Field(default="", max_length=240)

    @model_validator(mode="after")
    def typed_claim_has_count_and_evidence(self) -> NarrationFocusOutput:
        if self.focus == "unknown":
            if self.primary_subject_count is not None or self.visible_subject_count is not None:
                raise ValueError("unknown focus cannot carry a typed count")
            return self
        if self.primary_subject_count is None:
            raise ValueError("typed focus needs primary_subject_count")
        if self.focus == "single_subject" and self.primary_subject_count != 1:
            raise ValueError("single_subject requires primary_subject_count=1")
        if self.focus == "group" and self.primary_subject_count < 2:
            raise ValueError("group focus requires primary_subject_count>=2")
        if (
            self.visible_subject_count is not None
            and self.visible_subject_count < self.primary_subject_count
        ):
            raise ValueError("visible_subject_count cannot be below primary_subject_count")
        if not self.evidence_frame_ids:
            raise ValueError("typed focus needs evidence_frame_ids")
        return self


class NarrationFocusAgent(Agent[NarrationFocusInput, NarrationFocusOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.narration_focus",
        prompt_id="narration_focus",
        prompt_version="2026-09-07.3",
        model="gemini-2.5-flash",
        thinking_budget=256,
        timeout_s=45.0,
        max_attempts=2,
        backoff_s=(3.0,),
        enable_json_repair=True,
    )
    Input = NarrationFocusInput
    Output = NarrationFocusOutput

    def required_fields(self) -> list[str]:
        return []

    def media_uri(self, input: NarrationFocusInput) -> str | None:  # noqa: A002
        return input.frame_contact_sheet_uri

    def media_mime(self, input: NarrationFocusInput) -> str:  # noqa: A002
        return "image/jpeg"

    def render_prompt(self, input: NarrationFocusInput) -> str:  # noqa: A002
        return load_prompt(
            "narration_focus",
            asset_id=input.asset_id,
            source_instance_id=input.source_instance_id or "(none)",
            frame_samples_json=json.dumps(
                [sample.model_dump(mode="json") for sample in input.frame_samples],
                ensure_ascii=False,
            ),
            source_window_start_s=f"{input.source_window_start_s:.3f}",
            source_window_end_s=f"{input.source_window_end_s:.3f}",
        )

    def parse(
        self,
        raw_text: str,
        input: NarrationFocusInput,  # noqa: A002
    ) -> NarrationFocusOutput:
        try:
            payload = json.loads(raw_text)
            output = NarrationFocusOutput.model_validate(payload)
        except (TypeError, ValueError, ValidationError) as exc:
            raise SchemaError(f"narration_focus: malformed classifier output: {exc}") from exc
        known_frame_ids = {sample.sample_id for sample in input.frame_samples}
        unknown = set(output.evidence_frame_ids) - known_frame_ids
        if unknown:
            names = ", ".join(sorted(unknown))
            raise SchemaError(f"narration_focus: unknown evidence_frame_ids: {names}")
        if output.evidence_frame_ids and not output.evidence.strip():
            raise SchemaError("narration_focus: evidence is required with evidence_frame_ids")
        return output


def classify_narration_focus(
    client: ModelClient,
    *,
    asset_id: str,
    source_instance_id: str = "",
    frame_contact_sheet_uri: str,
    frame_samples: list[FocusFrame],
    source_window_start_s: float,
    source_window_end_s: float,
    ctx: RunContext | None = None,
) -> dict:
    """Run the bounded classifier and return materializer-ready typed fields.

    The caller must create ``frame_contact_sheet_uri`` from the pinned source
    generation and provide its exact sample IDs/times.  This function does not
    download media, choose frames, infer identity, or fall back to prose.
    """

    input = NarrationFocusInput(
        asset_id=asset_id,
        source_instance_id=source_instance_id,
        frame_contact_sheet_uri=frame_contact_sheet_uri,
        frame_samples=frame_samples,
        source_window_start_s=source_window_start_s,
        source_window_end_s=source_window_end_s,
    )
    output = NarrationFocusAgent(client).run(input, ctx=ctx)
    return {
        "asset_id": input.asset_id,
        "source_instance_id": input.source_instance_id,
        "single_subject": (
            True if output.focus == "single_subject" else False if output.focus == "group" else None
        ),
        "focus_mode": output.focus,
        "primary_subject_count": output.primary_subject_count,
        "visible_subject_count": output.visible_subject_count,
        "focus_evidence": {
            "source": "vision_sample_frames",
            "frame_ids": list(output.evidence_frame_ids),
            "source_window_start_s": input.source_window_start_s,
            "source_window_end_s": input.source_window_end_s,
            "detail": output.evidence,
        },
    }
