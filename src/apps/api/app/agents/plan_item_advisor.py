"""nova.plan.plan_item_advisor — per-item "Ask Kria" filming advice.

Stateless per turn (the full conversation history rides in each call), mirrors
InterviewerAgent. Read-only by design: the agent only ever proposes — the one
write path it can suggest (re-reading a clip with creator context) goes through
the clip-note PATCH route, which the frontend calls after explicit user consent.

Context (item brief, clips, conformance verdict, render phase, persona) is
injected as DATA with the standard anti-injection framing; the render-status
rule keeps the agent from confabulating progress ("is it done yet?" gets
answered from the provided phase, nothing more).
"""

from __future__ import annotations

import json
from typing import ClassVar

import structlog
from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.agents.interviewer_agent import ConversationTurn, _format_history
from app.agents.music_matcher import _sanitize_text
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

ADVISOR_PROMPT_VERSION = "2026-07-11-kria"

_MAX_SUGGESTED_NOTE = 140


class PlanItemAdvisorInput(BaseModel):
    turns: list[ConversationTurn] = Field(default_factory=list)
    theme: str = ""
    idea: str = ""
    edit_format: str = "montage"
    # Raw filming_guide dicts ({what, how, duration_s}) — formatted in render.
    filming_guide: list[dict] = Field(default_factory=list)
    # [{filename, shot_label, user_note}] — pre-shaped by the route.
    clips: list[dict] = Field(default_factory=list)
    # The persisted conformance verdict dict, or None.
    conformance: dict | None = None
    # Human-readable render phase ("no render yet", "rendering", "ready", "failed").
    job_phase: str = "no render yet"
    persona_summary: str = ""
    content_mode: str = "create_new"


class PlanItemAdvisorOutput(BaseModel):
    reply: str = Field(min_length=1)
    suggestions: list[str] = Field(default_factory=list, max_length=4)
    # Non-empty ONLY when the agent proposes re-reading a clip with this
    # distilled creator context (frontend asks consent, then PATCHes the note).
    suggested_note: str = ""


def _format_shots(filming_guide: list[dict]) -> str:
    lines = []
    for i, shot in enumerate(filming_guide):
        if not isinstance(shot, dict):
            continue
        what = _sanitize_text(str(shot.get("what", "") or ""))
        how = _sanitize_text(str(shot.get("how", "") or ""))
        dur = shot.get("duration_s", 1)
        line = f"  shot[{i + 1}]: {dur}s — {what}"
        if how:
            line += f" ({how})"
        lines.append(line)
    return "\n".join(lines) if lines else "  (no shot list — footage-selection item)"


def _format_clips(clips: list[dict]) -> str:
    lines = []
    for c in clips:
        if not isinstance(c, dict):
            continue
        name = _sanitize_text(str(c.get("filename", "") or "clip"))
        slot = _sanitize_text(str(c.get("shot_label", "") or "extra footage"))
        note = _sanitize_text(str(c.get("user_note", "") or ""))
        line = f"  - {name} → {slot}"
        if note:
            line += f' | creator note: "{note}"'
        lines.append(line)
    return "\n".join(lines) if lines else "  (no clips attached yet)"


def _format_conformance(conformance: dict | None) -> str:
    if not isinstance(conformance, dict) or not conformance.get("verdict"):
        return "  (no brief read yet)"
    # Skip verdicts the user has dismissed or suppressed — surfacing hidden
    # context to the advisor would quote context the user explicitly hid.
    if conformance.get("dismissed") or conformance.get("suppressed"):
        return "  (no brief read yet)"
    verdict = _sanitize_text(str(conformance.get("verdict", "")))
    summary = _sanitize_text(str(conformance.get("summary", "")))
    return f"  verdict: {verdict} — {summary}"


class PlanItemAdvisorAgent(Agent[PlanItemAdvisorInput, PlanItemAdvisorOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.plan_item_advisor",
        prompt_id="plan_item_advisor",
        prompt_version=ADVISOR_PROMPT_VERSION,
        model="gemini-2.5-flash",
        max_attempts=3,
        backoff_s=(2.0, 6.0),
        timeout_s=20.0,
        thinking_budget=512,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = PlanItemAdvisorInput
    Output = PlanItemAdvisorOutput
    response_json = True

    def required_fields(self) -> list[str]:
        return ["reply"]

    def render_prompt(self, input: PlanItemAdvisorInput) -> str:  # noqa: A002
        return load_prompt(
            "plan_item_advisor",
            theme=_sanitize_text(input.theme),
            idea=_sanitize_text(input.idea),
            edit_format=_sanitize_text(input.edit_format),
            shot_list=_format_shots(input.filming_guide),
            clips_block=_format_clips(input.clips),
            conformance_block=_format_conformance(input.conformance),
            job_phase=_sanitize_text(input.job_phase),
            persona_summary=_sanitize_text(input.persona_summary),
            content_mode=_sanitize_text(input.content_mode),
            history=_format_history(input.turns),
        )

    def parse(
        self,
        raw_text: str,
        input: PlanItemAdvisorInput,  # noqa: A002, ARG002
    ) -> PlanItemAdvisorOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"plan_item_advisor: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("plan_item_advisor: response is not a JSON object")
        try:
            output = PlanItemAdvisorOutput(**data)
        except ValidationError as exc:
            raise RefusalError(f"plan_item_advisor: validation — {exc}") from exc
        return PlanItemAdvisorOutput(
            reply=output.reply.strip(),
            suggestions=[s.strip() for s in output.suggestions if s and s.strip()][:4],
            suggested_note=output.suggested_note.strip()[:_MAX_SUGGESTED_NOTE],
        )

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: return ONLY valid JSON with keys: reply (string), "
            "suggestions (list of 2-3 short strings), suggested_note (string, "
            "usually empty). No markdown, no prose outside the JSON."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
