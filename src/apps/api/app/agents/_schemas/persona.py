"""Schemas for nova.plan.persona_generator.

The questionnaire is UNTRUSTED user free-text — every field is wrapped as DATA
in the prompt and sanitized before the model sees it. The `Persona` is the
editable AI output that later threads into `content_plan_generator` and
`intro_writer` (so it is re-sanitized again at the threading point — see the
plan's T7).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Bump when prompts/generate_persona.txt OR prompts/persona_archetypes.json OR
# prompts/tiktok_success_factors.json changes (CLAUDE.md prompt-change rule; the
# archetype bank + success-factor bank are part of the prompt).
# 2026-05-30.1 — added `rationale` (the AI's "why this lane" shown in the dashboard).
# 2026-05-30 — added $success_factors block + archetype performance ranking.
PERSONA_PROMPT_VERSION = "2026-05-30.1"

# Upper bounds keep a runaway model response from bloating the persona row.
_MAX_PILLARS = 8
_MAX_TOPICS = 20


class PersonaQuestionnaire(BaseModel):
    """Raw onboarding answers. All optional free-text; treated as untrusted DATA."""

    work: str = ""
    school: str = ""
    social: str = ""
    location: str = ""
    hobbies: str = ""
    travels: str = ""
    passions: str = ""
    # Optional handle the user can paste; never fetched in v1 (TikTok API deferred).
    tiktok_handle: str = ""


class Persona(BaseModel):
    """Editable AI persona. Shape is the contract the persona editor UI + the
    downstream content_plan_generator / intro_writer agents rely on."""

    summary: str = Field(min_length=1)
    content_pillars: list[str] = Field(min_length=1, max_length=_MAX_PILLARS)
    tone: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    posting_cadence: str = Field(min_length=1)
    sample_topics: list[str] = Field(default_factory=list, max_length=_MAX_TOPICS)
    # The AI's short "why this lane fits you + why it works on TikTok", surfaced
    # read-only in the dashboard. Optional so a user edit that drops it never
    # fails validation; the generator's prompt reliably fills it (structural-checked).
    rationale: str = ""

    def to_dict(self) -> dict:
        return self.model_dump()
