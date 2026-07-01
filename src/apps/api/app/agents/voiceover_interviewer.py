"""nova.voiceover.interviewer — ask up to 3 quick questions before writing a
voiceover script.

A NEW thin agent, NOT a reuse of `interviewer_agent` (that one is persona-coupled:
its input carries `tiktok_summary`, its prompt is creator onboarding, and its
`parse()` forces a "One last thing —" final-question prefix + a 5–8 turn arc —
`interviewer_agent.py:58-65,100-103,154-158`). This interview is short (hard cap 3),
seeded with the footage summary + brief, and exists only to tailor the script.

Follow-up TODO (rule of three): extract a shared `ConversationalInterviewer` base
once a third interview surface appears.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.agents.music_matcher import _sanitize_text

# Hard cap: the interview closes at this many turns regardless of what the model
# wants (the route also enforces it as the backstop). "A few questions", per the
# design review — not the onboarding interviewer's 5–8.
_MAX_TURNS = 3


class VoiceoverTurn(BaseModel):
    role: str  # "agent" | "user"
    content: str


class VoiceoverInterviewerInput(BaseModel):
    # Light footage summary + the creator's one-line brief — what the questions
    # should build on. UNTRUSTED (footage-derived / free text): framed as DATA.
    footage_summary: str = ""
    brief: str = ""
    turns: list[VoiceoverTurn] = Field(default_factory=list)
    turn_count: int = 0


class VoiceoverInterviewerOutput(BaseModel):
    question: str = Field(min_length=3)
    suggestions: list[str] = Field(default_factory=list, max_length=4)
    is_final: bool = False


def _format_history(turns: list[VoiceoverTurn]) -> str:
    if not turns:
        return "(no answers yet — this is the first question)"
    lines = []
    for t in turns:
        who = "YOU ASKED" if t.role == "agent" else "CREATOR SAID"
        lines.append(f"{who}: {_sanitize_text(t.content)}")
    return "\n".join(lines)


class VoiceoverInterviewerAgent(Agent[VoiceoverInterviewerInput, VoiceoverInterviewerOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.voiceover.interviewer",
        prompt_id="voiceover_interview",
        prompt_version="2026-07-01",
        model="gemini-2.5-flash",
        max_attempts=3,
        timeout_s=20.0,
        thinking_budget=1024,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = VoiceoverInterviewerInput
    Output = VoiceoverInterviewerOutput
    response_json = True

    def required_fields(self) -> list[str]:
        return ["question"]

    def render_prompt(self, input: VoiceoverInterviewerInput) -> str:  # noqa: A002
        from app.pipeline.prompt_loader import load_prompt  # noqa: PLC0415

        footage = _sanitize_text(input.footage_summary) or "(no footage summary available)"
        return load_prompt(
            "voiceover_interview",
            footage_summary=footage,
            brief=_sanitize_text(input.brief) or "(none)",
            history=_format_history(input.turns),
            turns_so_far=str(max(0, int(input.turn_count or 0))),
            max_turns=str(_MAX_TURNS),
        )

    def parse(
        self,
        raw_text: str,
        input: VoiceoverInterviewerInput,  # noqa: A002
    ) -> VoiceoverInterviewerOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"voiceover_interviewer: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("voiceover_interviewer: response is not a JSON object")

        try:
            out = VoiceoverInterviewerOutput(**data)
        except ValidationError as exc:
            raise RefusalError(f"voiceover_interviewer: validation — {exc}") from exc

        # Server-enforced cap: the model's is_final is honored, but the interview
        # ALSO closes once we've asked _MAX_TURNS questions, whatever the model wants.
        n = max(1, int(input.turn_count or 1))
        is_final = out.is_final or n >= _MAX_TURNS
        return VoiceoverInterviewerOutput(
            question=out.question,
            suggestions=out.suggestions,
            is_final=is_final,
        )

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: return ONLY valid JSON with keys: question (string), "
            "suggestions (list of 2-4 short strings), is_final (boolean). "
            "No markdown, no prose outside the JSON."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
