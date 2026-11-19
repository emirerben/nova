"""Schemas for nova.plan.content_plan_generator.

Input is the editable Persona (already sanitized at generation time, re-sanitized
into the prompt here) plus optional user free-text events and a horizon. Output
is a list of per-day PlanItemSpec the user can edit before generating videos.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.agents._prompt_fingerprint import prompt_fingerprint
from app.agents._schemas.edit_format import DEFAULT_EDIT_FORMAT, EditFormat, coerce_edit_format
from app.agents._schemas.persona import Persona

# Derived from the files the prompt is built from, so editing any of them moves the
# version and no PR edits a shared line (docs/pipelines/prompt-version-history.md).
# The banks are part of the prompt; the weekly research PR needs no version bump.
CONTENT_PLAN_PROMPT_SOURCES: tuple[str, ...] = (
    "app/agents/content_plan_generator.py",
    "app/agents/persona_examples.py",
    "app/agents/_schemas/content_plan.py",
    "app/kria/reply_language.py",
    "prompts/generate_content_plan.txt",
    "prompts/content_ideas.json",
    "prompts/tiktok_success_factors.json",
)
CONTENT_PLAN_PROMPT_VERSION = prompt_fingerprint("cp", CONTENT_PLAN_PROMPT_SOURCES)

DEFAULT_HORIZON_DAYS = 30
MAX_HORIZON_DAYS = 60

# Filming-guide constants — shared between the schema validator and parse().
MAX_SHOTS_PER_ITEM = 4
MIN_SHOT_DURATION_S = 1
MAX_SHOT_DURATION_S = 60


class ShotSpec(BaseModel):
    """One concrete shot in a filming guide.

    Best-effort: ``how`` is optional (an empty string is valid); ``duration_s``
    is clamped server-side so an out-of-range LLM value never raises.
    """

    what: str = Field(min_length=1)  # what to film ("wide shot of the dish being plated")
    how: str = ""  # angle / framing / movement ("handheld, eye level")
    duration_s: int = Field(ge=MIN_SHOT_DURATION_S, le=MAX_SHOT_DURATION_S)
    # How many clips the creator should film for this shot (default 1).
    clip_count: int = Field(default=1, ge=1, le=10)


class ContentPlanInput(BaseModel):
    persona: Persona
    # Optional free-text: trips, launches, exams the plan should lean into. UNTRUSTED.
    events: str = ""
    horizon_days: int = DEFAULT_HORIZON_DAYS
    # Feedback-loop rollup (Phase 2): a bounded, already-sanitized summary of the
    # creator's reactions + steer notes (services/feedback_summary). Empty for a
    # first generation; set on a user-triggered regenerate. Biases NEW ideas only —
    # the regenerate task preserves hand-edited days verbatim (the "their say" rule).
    preference_summary: str = ""
    # Internal-only (never user-set): on the constrained-regeneration pass, the
    # ideas already kept in the plan. The generator renders them as an explicit
    # "generate ideas DISTINCT from these" block so the second pass refills the
    # near-duplicate day slots with genuinely new ideas. Empty on the first pass.
    exclude_ideas: list[str] = Field(default_factory=list)
    # Deep TikTok analysis summary (analyze_tiktok_profile task). Pre-rendered
    # summary_for_prompts — the creator's own proven content ideas, hooks, and voice.
    # NOT stored on ContentPlan; injected at call time. Empty → byte-identical to baseline.
    tiktok_analysis: str = ""
    # Creator Agent M3: instruction verbosity for this user's plan items.
    # "full" (default) → byte-identical to pre-M3 baseline; "light"/"none" inject a
    # directive block. Gated on settings.user_style_enabled; defaults to "full" when
    # the flag is off or the style row is absent.
    instruction_level: Literal["full", "light", "none"] = "full"
    # Creator Agent M3: the user's declared edit-format preference weights (e.g.
    # {"montage": 0.6, "talking_head": 0.4}). Empty → byte-identical to baseline.
    preferred_edit_format_mix: dict[str, float] = Field(default_factory=dict)
    # M1 Bring-Your-Own-Ideas: the user's own content ideas, extracted from
    # Persona.idea_seeds[].text. Empty list → byte-identical to pre-M1 baseline
    # (no user-ideas block injected, so plans generated without seeds are
    # unchanged). Populated from the build path when seeds exist.
    user_idea_seeds: list[str] = Field(default_factory=list)
    # Account-wide creator direction, resolved at dispatch and sanitized by the
    # agent boundary. Empty means memory is disabled or has no active rows.
    creator_direction: str = ""
    # KRI-784: the creator's language ("tr"/"en"), from the chat or the caller. None:
    # read from the creator's own words below; English/unknown renders byte-identical.
    reply_language: str | None = Field(default=None, exclude_if=lambda value: value is None)


class PlanItemSpec(BaseModel):
    day_index: int = Field(ge=1, le=MAX_HORIZON_DAYS)
    theme: str = Field(min_length=1)
    idea: str = Field(min_length=1)
    filming_suggestion: str = ""
    # The AI's short "why this video works for you + which proven lever it pulls",
    # surfaced in the dashboard. Optional so a missing rationale never drops an
    # otherwise-good item (best-effort, like filming_suggestion).
    rationale: str = ""
    # The edit shape this idea is meant to become. Drives the render archetype
    # dispatch. Defaults to (and coerces unknowns to) montage so a missing or
    # drifted LLM value never drops the item.
    edit_format: EditFormat = DEFAULT_EDIT_FORMAT
    # Structured shot list, 2–4 shots keyed to the edit_format. Empty list is
    # valid (covers legacy items and malformed LLM output); best-effort like
    # filming_suggestion — a missing guide never drops an otherwise-good item.
    filming_guide: list[ShotSpec] = Field(default_factory=list)

    @field_validator("edit_format", mode="before")
    @classmethod
    def _coerce_edit_format(cls, v: object) -> EditFormat:
        return coerce_edit_format(v)


class ContentPlanOutput(BaseModel):
    items: list[PlanItemSpec] = Field(min_length=1)

    def to_dict(self) -> dict:
        return self.model_dump()
