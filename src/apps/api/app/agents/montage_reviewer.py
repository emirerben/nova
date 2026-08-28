"""Visually review model-authored montage windows before they are rendered."""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field, model_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt


class MontageReviewCutInput(BaseModel):
    cut_id: str = Field(min_length=1, max_length=100)
    media_id: str = Field(min_length=1, max_length=100)
    source_start_s: float = Field(ge=0)
    source_end_s: float = Field(gt=0)
    output_duration_s: float = Field(ge=0.4, le=3.0)

    @model_validator(mode="after")
    def validate_window(self) -> MontageReviewCutInput:
        if self.source_end_s <= self.source_start_s:
            raise ValueError("montage review source window must be positive")
        if abs((self.source_end_s - self.source_start_s) - self.output_duration_s) > 0.001:
            raise ValueError("montage review source window must match output duration")
        return self


class MontageReviewInput(BaseModel):
    file_uri: str = Field(min_length=1)
    source_media_id: str = Field(min_length=1, max_length=100)
    source_duration_s: float = Field(gt=0)
    creator_request: str = Field(default="", max_length=1000)
    proposed_cuts: list[MontageReviewCutInput] = Field(min_length=1, max_length=80)
    candidate_moments: list[dict] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_source_windows(self) -> MontageReviewInput:
        for cut in self.proposed_cuts:
            if cut.media_id != self.source_media_id:
                raise ValueError("montage review cuts must belong to the reviewed source")
            if cut.source_end_s > self.source_duration_s + 0.001:
                raise ValueError("montage review cut exceeds source duration")
        return self


class MontageCutReview(BaseModel):
    cut_id: str = Field(min_length=1, max_length=100)
    keep: bool
    quality_score: float = Field(ge=0, le=10)
    observed_action: str = Field(default="", max_length=160)
    feedback: str = Field(default="", max_length=280)
    replacement_start_s: float | None = Field(default=None, ge=0)
    replacement_end_s: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_replacement(self) -> MontageCutReview:
        if (self.replacement_start_s is None) != (self.replacement_end_s is None):
            raise ValueError("montage review replacement must provide both timestamps")
        if (
            self.replacement_start_s is not None
            and self.replacement_end_s is not None
            and self.replacement_end_s <= self.replacement_start_s
        ):
            raise ValueError("montage review replacement window must be positive")
        return self


class MontageReviewOutput(BaseModel):
    overall_score: float = Field(ge=0, le=10)
    needs_replan: bool
    cut_reviews: list[MontageCutReview] = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=500)


class MontageReviewAgent(Agent[MontageReviewInput, MontageReviewOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.montage_reviewer",
        prompt_id="montage_review",
        prompt_version="2026-08-28-v1",
        model="gemini-3.1-pro-preview",
        thinking_level="medium",
        timeout_s=90.0,
        max_attempts=2,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        enable_json_repair=True,
    )
    Input = MontageReviewInput
    Output = MontageReviewOutput
    response_json = True

    def media_uri(self, input: MontageReviewInput) -> str | None:  # noqa: A002
        return input.file_uri

    def required_fields(self) -> list[str]:
        return ["cut_reviews", "summary"]

    def render_prompt(self, input: MontageReviewInput) -> str:  # noqa: A002
        return load_prompt(
            "montage_review",
            source_media_id=input.source_media_id,
            source_duration_s=f"{input.source_duration_s:.3f}",
            creator_request=input.creator_request or "(none)",
            proposed_cuts=json.dumps(
                [cut.model_dump(mode="json") for cut in input.proposed_cuts],
                ensure_ascii=False,
            ),
            candidate_moments=json.dumps(input.candidate_moments, ensure_ascii=False),
        )

    def parse(self, raw_text: str, input: MontageReviewInput) -> MontageReviewOutput:  # noqa: A002
        try:
            payload = json.loads(raw_text)
            output = MontageReviewOutput.model_validate(payload)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"montage_reviewer: invalid JSON — {exc}") from exc

        expected = {cut.cut_id for cut in input.proposed_cuts}
        actual = {review.cut_id for review in output.cut_reviews}
        if len(output.cut_reviews) != len(input.proposed_cuts) or actual != expected:
            raise SchemaError("montage_reviewer: every proposed cut needs exactly one review")

        by_id = {cut.cut_id: cut for cut in input.proposed_cuts}
        for review in output.cut_reviews:
            if review.replacement_start_s is None or review.replacement_end_s is None:
                continue
            cut = by_id[review.cut_id]
            if review.replacement_end_s > input.source_duration_s + 0.001:
                raise SchemaError("montage_reviewer: replacement exceeds source duration")
            replacement_duration = review.replacement_end_s - review.replacement_start_s
            if abs(replacement_duration - cut.output_duration_s) > 0.15:
                raise SchemaError(
                    "montage_reviewer: replacement duration must stay close to the proposed cut"
                )
        return output
