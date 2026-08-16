"""Draft a complete, reviewable story from all uploaded plan-item media."""

from __future__ import annotations

import json
import re
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt

_SENSORY_CLAIM = re.compile(
    r"\b(?:delicious|tasty|flavorful|refreshing|favorite)\b",
    re.IGNORECASE,
)
_SENSORY_MODIFIER = re.compile(
    r"\b(?:delicious|tasty|flavorful|refreshing|favorite)\b(?=\s+\w)",
    re.IGNORECASE,
)
_PERSONAL_PRONOUN = re.compile(r"\b(?:i|we|my|our|us)\b", re.IGNORECASE)
_UNSUPPORTED_ACTION_LEAD = re.compile(
    r"^\s*(?:finally,?\s+)?(?:enjoying|discovering|relaxing|exploring|wandering|"
    r"visiting|tasting|trying)\b",
    re.IGNORECASE,
)


def minimum_required_sources(available: int) -> int:
    """Keep small edits varied without forcing one redundant source into the cut."""
    if available <= 3:
        return available
    if available < 7:
        return available - 1
    return 7


def _neutralize_sensory_modifier(text: str) -> str:
    cleaned = _SENSORY_MODIFIER.sub("", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = re.sub(r"\bA (?=[AEIOUaeiou])", "An ", cleaned)
    cleaned = re.sub(r"\ba (?=[AEIOUaeiou])", "an ", cleaned)
    cleaned = re.sub(r"\bAn (?=[^AEIOUaeiou\W])", "A ", cleaned)
    return re.sub(r"\ban (?=[^AEIOUaeiou\W])", "a ", cleaned)


def ai_draft_thought_has_unsupported_claim(text: str) -> bool:
    """Return whether model-authored copy asserts an unverified experience."""

    return bool(
        _PERSONAL_PRONOUN.search(text)
        or _UNSUPPORTED_ACTION_LEAD.search(text)
        or _SENSORY_CLAIM.search(text)
    )


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
    story_beats: list[DraftStoryBeat] = Field(min_length=1, max_length=5)


class EditProposalAgent(Agent[EditProposalAgentInput, EditProposalAgentOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.edit_proposal",
        prompt_id="edit_proposal",
        prompt_version="1.0.1",
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
        minimum = minimum_required_sources(len(input.media))
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
        minimum_topics = min(3, len(input.media))
        distinct_topics = {beat.topic.strip().casefold() for beat in output.story_beats}
        if len(distinct_topics) < minimum_topics:
            raise SchemaError(
                f"edit_proposal: story needs at least {minimum_topics} distinct topics"
            )
        media_by_id = {media.media_id: media for media in input.media}
        for beat in output.story_beats:
            has_creator_context = any(
                media_by_id[media_id].user_context.strip() for media_id in beat.media_ids
            )
            if not has_creator_context:
                beat.thought = _neutralize_sensory_modifier(beat.thought)
            if len(beat.thought.split()) > 18:
                raise SchemaError("edit_proposal: draft thought exceeds 18 words")
            if not has_creator_context and ai_draft_thought_has_unsupported_claim(beat.thought):
                raise SchemaError(
                    "edit_proposal: draft thought invents an unsupported personal experience"
                )
        if abs(output.duration_s - input.target_duration_s) > 5:
            raise SchemaError("edit_proposal: duration is too far from the creator's target")
        beat_duration = sum(beat.duration_s for beat in output.story_beats)
        max_intro_gap = max(6.0, output.duration_s * 0.3)
        if beat_duration > output.duration_s or output.duration_s - beat_duration > max_intro_gap:
            raise SchemaError("edit_proposal: beat durations do not fit the declared duration")
        return output
