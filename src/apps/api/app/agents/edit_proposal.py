"""Draft a complete, reviewable story from all uploaded plan-item media."""

from __future__ import annotations

import json
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt


class EditProposalMedia(BaseModel):
    media_id: str
    lane: Literal["clip", "asset"]
    kind: Literal["image", "video"]
    source_filename: str = ""
    duration_s: float | None = None
    user_context: str = ""
    subject: str = ""
    description: str = ""
    on_screen_text: str = ""
    best_moments: list[dict] = Field(default_factory=list)


class EditProposalAgentInput(BaseModel):
    idea: str = ""
    theme: str = ""
    direction: Literal["guided_story", "fast_montage", "text_explainer"]
    goal: str = ""
    pace: Literal["relaxed", "balanced", "fast"]
    target_duration_s: int = Field(ge=10, le=60)
    media: list[EditProposalMedia] = Field(min_length=1, max_length=60)


class DraftStoryBeat(BaseModel):
    topic: str = Field(min_length=1, max_length=80)
    thought: str = Field(default="", max_length=280)
    media_ids: list[str] = Field(min_length=1, max_length=4)
    layout: Literal["fullscreen", "supporting_card"] = "fullscreen"
    duration_s: float = Field(ge=1.0, le=12.0)


class EditProposalAgentOutput(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    duration_s: int = Field(ge=10, le=60)
    story_beats: list[DraftStoryBeat] = Field(min_length=1, max_length=20)


class EditProposalAgent(Agent[EditProposalAgentInput, EditProposalAgentOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.edit_proposal",
        prompt_id="edit_proposal",
        prompt_version="1.0.0",
        model="gemini-2.5-flash",
        thinking_budget=1024,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        enable_json_repair=True,
    )
    Input = EditProposalAgentInput
    Output = EditProposalAgentOutput
    response_json = True

    def required_fields(self) -> list[str]:
        return ["title", "story_beats"]

    def render_prompt(self, input: EditProposalAgentInput) -> str:  # noqa: A002
        return load_prompt(
            "edit_proposal",
            idea=input.idea[:500],
            theme=input.theme[:500],
            direction=input.direction,
            goal=input.goal[:500]
            or "Make the uploaded material feel intentional and worth sharing.",
            pace=input.pace,
            target_duration_s=str(input.target_duration_s),
            media_json=json.dumps([row.model_dump() for row in input.media], ensure_ascii=False),
        )

    def parse(
        self,
        raw_text: str,
        input: EditProposalAgentInput,  # noqa: A002
    ) -> EditProposalAgentOutput:
        try:
            output = EditProposalAgentOutput.model_validate(json.loads(raw_text))
        except Exception as exc:  # noqa: BLE001
            raise SchemaError(f"edit_proposal: invalid output — {exc}") from exc
        allowed = {m.media_id for m in input.media}
        used: set[str] = set()
        for beat in output.story_beats:
            if not set(beat.media_ids) <= allowed:
                raise SchemaError("edit_proposal: beat references unknown media")
            if len(beat.media_ids) != len(set(beat.media_ids)):
                raise SchemaError("edit_proposal: beat repeats the same media")
            used.update(beat.media_ids)
        minimum = min(7, len(input.media))
        if len(used) < minimum:
            raise SchemaError(
                f"edit_proposal: selected {len(used)} distinct sources; need at least {minimum}"
            )
        available_kinds = {media.kind for media in input.media}
        used_kinds = {media.kind for media in input.media if media.media_id in used}
        if len(available_kinds) > 1 and used_kinds != available_kinds:
            raise SchemaError("edit_proposal: story must use both photos and videos")
        if input.direction in {"guided_story", "text_explainer"}:
            minimum_beats = min(3, len(input.media))
            if len(output.story_beats) < minimum_beats:
                raise SchemaError(
                    f"edit_proposal: guided story needs at least {minimum_beats} beats"
                )
            if any(not beat.thought.strip() for beat in output.story_beats):
                raise SchemaError("edit_proposal: guided story thoughts cannot be empty")
        if abs(output.duration_s - input.target_duration_s) > 5:
            raise SchemaError("edit_proposal: duration is too far from the creator's target")
        beat_duration = sum(beat.duration_s for beat in output.story_beats)
        max_intro_gap = max(6.0, output.duration_s * 0.3)
        if beat_duration > output.duration_s or output.duration_s - beat_duration > max_intro_gap:
            raise SchemaError("edit_proposal: beat durations do not fit the declared duration")
        return output
