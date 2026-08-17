"""Conversational creative-direction agent for guided edits.

The agent has two jobs: turn natural language into the existing typed proposal
brief before analysis, and revise the editorial fields of a reviewable draft.
Media identity never comes from this agent; review revisions are rejoined with
the server-owned media and beat membership by the route.
"""

from __future__ import annotations

import json
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents.edit_proposal import ai_draft_thought_has_unsupported_claim
from app.agents.prompt_safety import sanitize_prompt_text
from app.pipeline.prompt_loader import load_prompt
from app.schemas.edit_proposal import (
    EDIT_CONVERSATION_MAX_TURNS,
    EditConversationTurn,
    ProposalBrief,
)


class EditGuideMediaSummary(BaseModel):
    kind: Literal["image", "video"]
    source_filename: str = ""
    creator_context: str = ""
    subject: str = ""
    description: str = ""


class EditGuideBeatInput(BaseModel):
    beat_id: str
    topic: str
    thought: str = ""
    thought_source: Literal["ai_draft", "user"] = "ai_draft"
    layout: Literal["fullscreen", "supporting_card"] = "fullscreen"
    duration_s: float
    media_count: int = Field(ge=1)


class EditGuideRevisionBeat(BaseModel):
    beat_id: str = Field(min_length=1, max_length=100)
    topic: str = Field(min_length=1, max_length=80)
    thought: str = Field(default="", max_length=280)
    layout: Literal["fullscreen", "supporting_card"] = "fullscreen"
    duration_s: float = Field(ge=1.0, le=12.0)


class EditGuideRevision(BaseModel):
    direction: Literal["guided_story", "fast_montage", "text_explainer"]
    goal: str = Field(default="", max_length=500)
    pace: Literal["relaxed", "balanced", "fast"]
    duration_s: int = Field(ge=10, le=60)
    title: str = Field(min_length=1, max_length=100)
    story_beats: list[EditGuideRevisionBeat] = Field(min_length=1, max_length=20)


class EditGuideInput(BaseModel):
    phase: Literal["briefing", "review"]
    idea: str = ""
    theme: str = ""
    turns: list[EditConversationTurn] = Field(
        default_factory=list, max_length=EDIT_CONVERSATION_MAX_TURNS
    )
    brief: ProposalBrief = Field(default_factory=ProposalBrief)
    media: list[EditGuideMediaSummary] = Field(default_factory=list, max_length=60)
    title: str = ""
    beats: list[EditGuideBeatInput] = Field(default_factory=list, max_length=20)


class EditGuideOutput(BaseModel):
    reply: str = Field(min_length=1, max_length=600)
    suggestions: list[str] = Field(default_factory=list, max_length=3)
    brief: ProposalBrief
    ready_to_plan: bool = False
    revision: EditGuideRevision | None = None


def _format_turns(turns: list[EditConversationTurn]) -> str:
    if not turns:
        return "(no prior conversation)"
    lines = []
    for turn in turns:
        label = "CREATOR" if turn.role == "user" else "KRIA"
        lines.append(f"{label}: {sanitize_prompt_text(turn.content, limit=1000)}")
    return "\n".join(lines)


class EditGuideAgent(Agent[EditGuideInput, EditGuideOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.edit_guide",
        prompt_id="edit_guide",
        prompt_version="1.0.3",
        model="gemini-2.5-flash",
        # Stay below the web proxy's 60s hard budget even when both attempts
        # reach their timeout. This prevents a late invisible DB commit after
        # the browser has already received a gateway timeout.
        max_attempts=2,
        backoff_s=(2.0,),
        timeout_s=20.0,
        thinking_budget=768,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        enable_json_repair=True,
    )
    Input = EditGuideInput
    Output = EditGuideOutput
    response_json = True
    max_output_tokens = 3000

    def required_fields(self) -> list[str]:
        return ["reply", "brief"]

    def render_prompt(self, input: EditGuideInput) -> str:  # noqa: A002
        current_plan = {
            "title": input.title,
            "beats": [beat.model_dump(mode="json") for beat in input.beats],
        }
        return load_prompt(
            "edit_guide",
            phase=input.phase,
            idea=sanitize_prompt_text(input.idea, limit=500),
            theme=sanitize_prompt_text(input.theme, limit=500),
            current_brief=json.dumps(input.brief.model_dump(mode="json"), ensure_ascii=False),
            media_json=json.dumps(
                [row.model_dump(mode="json") for row in input.media], ensure_ascii=False
            ),
            current_plan_json=json.dumps(current_plan, ensure_ascii=False),
            history=_format_turns(input.turns),
        )

    def parse(self, raw_text: str, input: EditGuideInput) -> EditGuideOutput:  # noqa: A002
        try:
            output = EditGuideOutput.model_validate(json.loads(raw_text))
        except (ValueError, TypeError, ValidationError) as exc:
            raise SchemaError(f"edit_guide: invalid output — {exc}") from exc

        reply = output.reply.strip()
        if not reply:
            raise SchemaError("edit_guide: reply cannot be blank")
        suggestions = [value.strip()[:100] for value in output.suggestions if value.strip()][:3]
        user_turns = sum(1 for turn in input.turns if turn.role == "user")
        ready_to_plan = output.ready_to_plan or user_turns >= 3

        if input.phase == "briefing" and output.revision is not None:
            raise SchemaError("edit_guide: briefing response cannot revise a draft")
        if input.phase == "review":
            if output.revision is None:
                # A clarification is conversational only. Models sometimes
                # restate the brief while asking a question; normalize that
                # harmless drift instead of spending the retry budget or
                # splitting it from the still-authoritative draft.
                output = output.model_copy(update={"brief": input.brief})
            if output.revision is not None:
                expected_ids = [beat.beat_id for beat in input.beats]
                actual_ids = [beat.beat_id for beat in output.revision.story_beats]
                if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected_ids):
                    raise SchemaError(
                        "edit_guide: revision must preserve every existing story beat exactly once"
                    )
                existing_by_id = {beat.beat_id: beat for beat in input.beats}
                for beat in output.revision.story_beats:
                    if len(beat.thought.split()) > 18:
                        raise SchemaError("edit_guide: revised thought exceeds 18 words")
                    if existing_by_id[
                        beat.beat_id
                    ].thought_source != "user" and ai_draft_thought_has_unsupported_claim(
                        beat.thought
                    ):
                        raise SchemaError(
                            "edit_guide: revision invents an unsupported personal experience"
                        )
            ready_to_plan = True

        # Answer chips are only useful while Kria is asking the creator a question.
        # Models sometimes return generic affirmations even after the brief is complete;
        # suppress those rather than presenting dead-end choices in the UI.
        if ready_to_plan or "?" not in reply:
            suggestions = []

        return output.model_copy(
            update={
                "reply": reply,
                "suggestions": suggestions,
                "ready_to_plan": ready_to_plan,
            }
        )

    def schema_clarification(self) -> str:
        return (
            "\n\nReturn only the documented JSON. In review mode, preserve every exact beat_id "
            "once; omit revision when you are only asking a clarifying question."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
