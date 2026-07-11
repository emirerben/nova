"""nova.plan.idea_expander — expand a bare idea into a filmable plan item.

Propose-only: the agent never writes to the DB. The route returns its output
for the frontend to display in a propose/accept card; the user explicitly
accepts before the PATCH write happens.
"""

from __future__ import annotations

import json
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.agents.music_matcher import _sanitize_text
from app.pipeline.prompt_loader import load_prompt

IDEA_EXPANDER_PROMPT_VERSION = "2026-07-11"

IdeaExpandVideoType = Literal["montage", "voiceover", "talking_to_camera"]
IdeaExpandContentMode = Literal["create_new", "existing_footage", "mixed"]


class IdeaExpanderInput(BaseModel):
    idea: str
    persona_summary: str = ""
    content_pillars: list[str] = Field(default_factory=list)
    creator_context: str = ""
    video_type: IdeaExpandVideoType = "montage"
    content_mode: IdeaExpandContentMode = "create_new"


class FilmingShot(BaseModel):
    what: str
    how: str
    duration_s: int = Field(default=3, ge=1, le=30)


class IdeaExpanderOutput(BaseModel):
    theme: str = Field(min_length=1, max_length=80)
    filming_suggestion: str = Field(min_length=1, max_length=300)
    filming_guide: list[FilmingShot] = Field(default_factory=list, max_length=4)
    rationale: str = Field(default="", max_length=400)


class EmptyFilmingGuideError(RefusalError, SchemaError):
    """Retryable empty-guide error: refusal semantics, schema retry path."""


class IdeaExpanderAgent(Agent[IdeaExpanderInput, IdeaExpanderOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.idea_expander",
        prompt_id="idea_expander",
        prompt_version=IDEA_EXPANDER_PROMPT_VERSION,
        model="gemini-2.5-flash",
        max_attempts=3,
        backoff_s=(2.0, 6.0),
        timeout_s=20.0,
        thinking_budget=512,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = IdeaExpanderInput
    Output = IdeaExpanderOutput
    response_json = True

    def required_fields(self) -> list[str]:
        return ["theme", "filming_suggestion", "filming_guide"]

    def render_prompt(self, input: IdeaExpanderInput) -> str:  # noqa: A002
        pillars_block = (
            "\n".join(f"  - {_sanitize_text(p)}" for p in input.content_pillars)
            if input.content_pillars
            else "  (none)"
        )
        return load_prompt(
            "idea_expander",
            idea=_sanitize_text(input.idea),
            persona_summary=_sanitize_text(input.persona_summary),
            content_pillars=pillars_block,
            creator_context=_sanitize_text(input.creator_context),
            video_type=input.video_type,
            content_mode=input.content_mode,
        )

    def parse(
        self,
        raw_text: str,
        input: IdeaExpanderInput,  # noqa: A002, ARG002
    ) -> IdeaExpanderOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"idea_expander: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("idea_expander: response is not a JSON object")
        try:
            output = IdeaExpanderOutput(**data)
        except ValidationError as exc:
            raise RefusalError(f"idea_expander: validation — {exc}") from exc
        shots = [
            FilmingShot(
                what=s.what.strip(),
                how=s.how.strip(),
                duration_s=s.duration_s,
            )
            for s in output.filming_guide
            if s.what.strip()
        ][:4]
        min_shots = 1 if input.video_type == "talking_to_camera" else 2
        if len(shots) < min_shots:
            expected = "1-4" if min_shots == 1 else "2-4"
            raise EmptyFilmingGuideError(
                f"idea_expander: filming_guide must contain {expected} concrete shots"
            )
        return IdeaExpanderOutput(
            theme=output.theme.strip(),
            filming_suggestion=output.filming_suggestion.strip(),
            filming_guide=shots,
            rationale=output.rationale.strip(),
        )

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: return ONLY valid JSON with keys: "
            "theme (string, ≤80 chars), "
            "filming_suggestion (string, ≤300 chars), "
            "filming_guide (list of non-empty shots; montage/voiceover need 2-4, "
            "talking_to_camera may use 1-4; each shot MUST include "
            "what, how, and duration_s), "
            "rationale (string, ≤400 chars). "
            "No markdown, no prose outside the JSON."
        )
