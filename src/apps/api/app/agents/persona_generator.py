"""nova.plan.persona_generator — turn an onboarding questionnaire into an
editable creator PERSONA.

Off-Job agent (no media). Runs once per user when onboarding starts, re-runnable
if the user edits the questionnaire. Output is stored on `personas.persona`
(JSONB) and threads into `content_plan_generator` + `intro_writer` later.

Security: the questionnaire is UNTRUSTED user free-text. Every field is
sanitized with `_sanitize_text` (strips control chars, role markers, code
fences) BEFORE it reaches the prompt, and the prompt frames it as DATA. Output
fields are sanitized again on the way out (defense-in-depth — the persona text
becomes prompt input to other agents downstream).
"""

from __future__ import annotations

import json
from typing import ClassVar

import structlog
from pydantic import ValidationError

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.agents._schemas.persona import (
    PERSONA_PROMPT_VERSION,
    Persona,
    PersonaQuestionnaire,
)
from app.agents.music_matcher import _sanitize_text
from app.agents.persona_examples import format_archetypes
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()


class PersonaGeneratorAgent(Agent[PersonaQuestionnaire, Persona]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.persona_generator",
        prompt_id="generate_persona",
        prompt_version=PERSONA_PROMPT_VERSION,
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = PersonaQuestionnaire
    Output = Persona

    def required_fields(self) -> list[str]:
        return ["summary", "content_pillars", "tone", "audience", "posting_cadence"]

    def render_prompt(self, input: PersonaQuestionnaire) -> str:  # noqa: A002
        return load_prompt(
            "generate_persona",
            work=_sanitize_text(input.work),
            school=_sanitize_text(input.school),
            social=_sanitize_text(input.social),
            location=_sanitize_text(input.location),
            hobbies=_sanitize_text(input.hobbies),
            travels=_sanitize_text(input.travels),
            passions=_sanitize_text(input.passions),
            tiktok_handle=_sanitize_text(input.tiktok_handle),
            # Curated market-research style types (reference, not user data).
            archetypes=format_archetypes(),
        )

    def parse(
        self,
        raw_text: str,
        input: PersonaQuestionnaire,  # noqa: A002, ARG002
    ) -> Persona:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"persona_generator: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("persona_generator: response is not a JSON object")

        try:
            persona = Persona(**data)
        except ValidationError as exc:
            # Missing/empty required field or list too long — retryable.
            raise RefusalError(f"persona_generator: persona validation — {exc}") from exc

        # Re-sanitize every output field. The model echoes user free-text back;
        # this text is later interpolated into other agents' prompts, so strip
        # injection vectors here too (defense-in-depth, plan T7).
        cleaned = Persona(
            summary=_sanitize_text(persona.summary),
            tone=_sanitize_text(persona.tone),
            audience=_sanitize_text(persona.audience),
            posting_cadence=_sanitize_text(persona.posting_cadence),
            content_pillars=[p for p in (_sanitize_text(x) for x in persona.content_pillars) if p],
            sample_topics=[t for t in (_sanitize_text(x) for x in persona.sample_topics) if t],
        )
        if not cleaned.content_pillars:
            raise RefusalError("persona_generator: content_pillars empty after sanitize")
        return cleaned

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: Return ONLY the JSON object with keys summary, "
            "content_pillars, tone, audience, posting_cadence, sample_topics. "
            "content_pillars MUST have 3-5 short items; sample_topics 5-8. "
            "No markdown, no text outside the JSON."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
