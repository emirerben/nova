"""Schemas for nova.plan.content_plan_generator.

Input is the editable Persona (already sanitized at generation time, re-sanitized
into the prompt here) plus optional user free-text events and a horizon. Output
is a list of per-day PlanItemSpec the user can edit before generating videos.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents._schemas.persona import Persona

# Bump when prompts/generate_content_plan.txt OR prompts/content_ideas.json OR
# prompts/tiktok_success_factors.json changes (CLAUDE.md prompt-change rule; the
# idea bank + success-factor bank are part of the prompt).
# 2026-05-30.1 — added per-item `rationale` (the AI's "why this video works",
#                shown in the dashboard).
# 2026-05-30 — added $success_factors block + performance-weighted idea ranking.
CONTENT_PLAN_PROMPT_VERSION = "2026-05-30.1"

DEFAULT_HORIZON_DAYS = 30
MAX_HORIZON_DAYS = 60


class ContentPlanInput(BaseModel):
    persona: Persona
    # Optional free-text: trips, launches, exams the plan should lean into. UNTRUSTED.
    events: str = ""
    horizon_days: int = DEFAULT_HORIZON_DAYS


class PlanItemSpec(BaseModel):
    day_index: int = Field(ge=1, le=MAX_HORIZON_DAYS)
    theme: str = Field(min_length=1)
    idea: str = Field(min_length=1)
    filming_suggestion: str = ""
    # The AI's short "why this video works for you + which proven lever it pulls",
    # surfaced in the dashboard. Optional so a missing rationale never drops an
    # otherwise-good item (best-effort, like filming_suggestion).
    rationale: str = ""


class ContentPlanOutput(BaseModel):
    items: list[PlanItemSpec] = Field(min_length=1)

    def to_dict(self) -> dict:
        return self.model_dump()
