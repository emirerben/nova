"""nova.plan.interviewer — adaptive onboarding chat Q generation.

Called once per chat turn. Takes the full conversation history + any scraped
TikTok profile and returns the next question to ask, suggestion chips, and a
flag signaling when the interview is complete.

Design:
- Single-shot per turn: no persistent session, just full history on each call.
- Hard cap: route enforces ≤8 turns; agent signals is_final=True when it
  decides no more questions are needed (typically 4-7 turns).
- Final-Q prefix: when is_final=True, the question MUST start with
  "One last thing — " (checked by the route).
- TikTok-aware: when tiktok_profile is present, the agent skips questions
  that the profile already answers and goes deeper on gaps.

Changelog:
  2026-06-08 — Turn 3 audience question rewritten to remove 'secretly filming for' framing.
"""

from __future__ import annotations

import json
from typing import ClassVar

import structlog
from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.agents.music_matcher import _sanitize_text
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

INTERVIEWER_PROMPT_VERSION = "2026-06-08"
_HARD_CAP = 8


# ── Schemas ───────────────────────────────────────────────────────────────────


class ConversationTurn(BaseModel):
    role: str  # "agent" | "user"
    content: str


class InterviewerInput(BaseModel):
    """Per-turn input for the interviewer agent."""

    turns: list[ConversationTurn] = Field(default_factory=list)
    # Sanitized TikTok profile summary — None when user skipped or scrape failed
    tiktok_summary: str | None = None
    turn_count: int = 0


class InterviewerOutput(BaseModel):
    """The next question to show the user."""

    question: str = Field(min_length=5)
    suggestions: list[str] = Field(default_factory=list, max_length=5)
    is_final: bool = False
    # "~3 OF ~6" eyebrow label shown above the question
    turn_label: str = ""


# ── Agent ─────────────────────────────────────────────────────────────────────


def _format_history(turns: list[ConversationTurn]) -> str:
    if not turns:
        return "(no prior conversation)"
    lines = []
    for t in turns:
        label = "INTERVIEWER" if t.role == "agent" else "CREATOR"
        lines.append(f"{label}: {_sanitize_text(t.content)}")
    return "\n".join(lines)


def _format_tiktok_context(summary: str | None) -> str:
    if not summary:
        return "(no TikTok profile — ask everything from scratch)"
    return (
        "TIKTOK PROFILE (already scraped — skip questions this already answers):\n"
        f"{_sanitize_text(summary)}"
    )


class InterviewerAgent(Agent[InterviewerInput, InterviewerOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.interviewer",
        prompt_id="interviewer",
        prompt_version=INTERVIEWER_PROMPT_VERSION,
        model="gemini-2.5-flash",
        max_attempts=3,
        backoff_s=(2.0, 6.0),
        timeout_s=20.0,
        thinking_budget=1024,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = InterviewerInput
    Output = InterviewerOutput
    response_json = True

    def required_fields(self) -> list[str]:
        return ["question"]

    def render_prompt(self, input: InterviewerInput) -> str:  # noqa: A002
        turns_so_far = input.turn_count
        return load_prompt(
            "interviewer",
            history=_format_history(input.turns),
            tiktok_context=_format_tiktok_context(input.tiktok_summary),
            turns_so_far=str(turns_so_far),
            hard_cap=str(_HARD_CAP),
        )

    def parse(
        self,
        raw_text: str,
        input: InterviewerInput,  # noqa: A002, ARG002
    ) -> InterviewerOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"interviewer: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("interviewer: response is not a JSON object")

        try:
            output = InterviewerOutput(**data)
        except ValidationError as exc:
            raise RefusalError(f"interviewer: validation — {exc}") from exc

        # Ensure final-Q prefix is present when agent says is_final
        if output.is_final and not output.question.startswith("One last thing"):
            output = InterviewerOutput(
                question=f"One last thing — {output.question}",
                suggestions=output.suggestions,
                is_final=True,
                turn_label=output.turn_label,
            )

        return output

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: return ONLY valid JSON with keys: "
            "question (string), suggestions (list of 2-4 short strings), "
            "is_final (boolean), turn_label (string like '~3 OF ~6'). "
            "No markdown, no prose outside the JSON."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
