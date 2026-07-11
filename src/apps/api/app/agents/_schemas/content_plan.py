"""Schemas for nova.plan.content_plan_generator.

Input is the editable Persona (already sanitized at generation time, re-sanitized
into the prompt here) plus optional user free-text events and a horizon. Output
is a list of per-day PlanItemSpec the user can edit before generating videos.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.agents._schemas.edit_format import DEFAULT_EDIT_FORMAT, EditFormat, coerce_edit_format
from app.agents._schemas.persona import Persona

# Bump when prompts/generate_content_plan.txt OR prompts/content_ideas.json OR
# prompts/tiktok_success_factors.json changes (CLAUDE.md prompt-change rule; the
# idea bank + success-factor bank are part of the prompt).
# 2026-05-31 — anti-cringe guardrails: ideas must be filmable real-life moments;
#              banned thought-leadership / forced-insight framing (the "what the
#              Champions League final taught me about business" class).
# 2026-05-30.2 — added $preferences block (feedback-loop preference_summary) so a
#                user-triggered regenerate biases new ideas toward what they liked.
# 2026-05-30.1 — added per-item `rationale` (the AI's "why this video works",
#                shown in the dashboard).
# 2026-05-30 — added $success_factors block + performance-weighted idea ranking.
# 2026-05-31.1 — emit per-item edit_format (montage|talking_head|day_vlog|
#                single_hero) so the plan's intended shape reaches the render
#                archetype dispatch instead of being inferred from clips alone.
# 2026-06-01 — added $variety_constraint block: code detects near-duplicate ideas
#              post-generation and re-invokes the generator once with the kept
#              ideas as an explicit "avoid these" list (the model won't self-impose
#              variety in one pass). Block is empty on the first pass, so that
#              render stays the proven baseline.
# 2026-06-05 — posts_per_week: plan now emits ~N items per 7-day window instead of
#              one item per calendar day. Prompt uses $posts_per_week + $target_item_count;
#              parse() enforces a per-week cap server-side. Also fixed the edit_format
#              parse-threading bug (items were silently defaulting to montage regardless
#              of what the model emitted).
# 2026-06-01.1 — weekly research refresh: content_ideas.json bumped to 2026-05-31
#                (new market-research ideas). Bump invalidates the planner cache so
#                the new ideas take effect on top of the dedup block.
# 2026-06-05.1 — per-item filming_guide: 2-4 shots each with {what, how, duration_s},
#                keyed to edit_format so montage/day_vlog/talking_head/single_hero each
#                get the right shot shape. Stored on PlanItem as JSONB; rendered on the
#                item detail page. filming_suggestion stays (feeds clip_plan_matcher).
# 2026-06-06 — added $tiktok_analysis block (deep TikTok profile analysis from
#              analyze_tiktok_profile task — creator's own proven ideas/hooks/voice).
#              Absent when analysis hasn't landed → prompt byte-identical to baseline.
# 2026-06-07 — Creator Agent M3: added $instruction_level and $edit_format_mix
#              placeholders (gated on user_style_enabled; empty string when style
#              absent → byte-identical to pre-M3 baseline).
# 2026-06-07.1 — weekly research refresh: content_ideas/persona/overlay/success-factors
#                banks bumped (adventure-humor, implied-question, serial-brand-chapter,
#                art-cultural-moment ideas; adventure-humor-scaleup-02 overlay).
# 2026-06-08 — retrospective-footage rule: past-event ideas emit empty filming_guide
#              + footage-selection filming_suggestion instead of a shot list.
# 2026-06-11 — direction-aware planning: $direction_lines (goal + current situation in
#              the persona block), $content_mode_block (existing_footage/mixed
#              directives; "" for create_new → near-baseline), and the static
#              past-trips-are-edit-material rule (the Buenos Aires incident: planner
#              assumed the creator lives where past-trip footage was shot).
# 2026-06-13 — M1 Bring-Your-Own-Ideas: $user_ideas block added above IDEA_BANK.
#              The block is rendered ONLY when the user has provided idea seeds via
#              Persona.idea_seeds → byte-identical to baseline when seeds are absent.
#              Directive: prefer and deepen the user's own ideas first; use the
#              market IDEA_BANK only to fill remaining slots.
CONTENT_PLAN_PROMPT_VERSION = "2026-07-11-kria"

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
