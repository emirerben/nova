"""Schemas for nova.plan.persona_generator.

The questionnaire is UNTRUSTED user free-text — every field is wrapped as DATA
in the prompt and sanitized before the model sees it. The `Persona` is the
editable AI output that later threads into `content_plan_generator` and
`intro_writer` (so it is re-sanitized again at the threading point — see the
plan's T7).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

# Bump when prompts/generate_persona.txt OR prompts/persona_archetypes.json OR
# prompts/tiktok_success_factors.json changes (CLAUDE.md prompt-change rule; the
# archetype bank + success-factor bank are part of the prompt).
# 2026-06-11 — added goal, content_mode (existing_footage|create_new|mixed) and
#              current_situation outputs: the interview now forks on "footage you
#              have vs videos you'll film" and grounds WHERE the creator's daily
#              life happens (stops the planner assuming they live where past-trip
#              footage was shot — the Buenos Aires incident).
# 2026-06-06.1 — added $tiktok_analysis block (deep TikTok profile analysis from
#                analyze_tiktok_profile task — creator's own proven hooks/themes/voice).
#                Injected call-time only; not stored on the questionnaire row. Absent
#                when the analysis hasn't landed → prompt byte-identical to baseline.
# 2026-06-06 — interview_turns replaces flat fields as primary input; added
#              signature_quote output field for the aha-moment reveal.
# 2026-06-05 — added posts_per_week (int 1-7) so the plan agent can emit the right
#              number of ideas per week; resolve_posts_per_week() provides a legacy
#              fallback for personas that predate this field.
# 2026-05-31 — concrete-pillar constraint: content_pillars + sample_topics must be
#              filmable real-life moments, never abstract concepts ("Analytical
#              Thinking") — stops the abstract pillar that seeds plan-agent cringe.
# 2026-05-30.2 — added $preferences block (feedback-loop preference_summary) so
#                "update persona from feedback" re-tunes the lane toward what works.
# 2026-05-30.1 — added `rationale` (the AI's "why this lane" shown in the dashboard).
# 2026-05-30 — added $success_factors block + archetype performance ranking.
PERSONA_PROMPT_VERSION = "2026-06-28"

# Upper bounds keep a runaway model response from bloating the persona row.
_MAX_PILLARS = 8
_MAX_TOPICS = 20


class InterviewTurn(BaseModel):
    role: str  # "agent" | "user"
    content: str


class PersonaQuestionnaire(BaseModel):
    """Raw onboarding answers. All optional free-text; treated as untrusted DATA."""

    work: str = ""
    school: str = ""
    social: str = ""
    location: str = ""
    hobbies: str = ""
    travels: str = ""
    passions: str = ""
    # Optional handle the user can paste; set by TikTok pre-screen.
    tiktok_handle: str = ""
    # Chat interview turns — set by the new onboarding chat flow.
    # Takes precedence over the flat fields above when present.
    interview_turns: list[InterviewTurn] = []
    # Feedback-loop rollup (Phase 2): empty on first onboarding; set only when the
    # user clicks "update persona from feedback" (services/feedback_summary). Steers
    # the regenerated lane toward what they reacted well to. NOT stored on the
    # questionnaire row — injected at call time by retune_persona_from_feedback.
    preference_summary: str = ""
    # Deep TikTok analysis summary (analyze_tiktok_profile task). Pre-rendered
    # summary_for_prompts from TikTokAnalysis — the creator's own proven hooks,
    # voice, and winning themes. NOT stored on the questionnaire row — injected at
    # call time by generate_persona and retune_persona_from_feedback. Empty when
    # the analysis hasn't landed yet (race) → prompt byte-identical to baseline.
    tiktok_analysis: str = ""


class Persona(BaseModel):
    """Editable AI persona. Shape is the contract the persona editor UI + the
    downstream content_plan_generator / intro_writer agents rely on."""

    summary: str = Field(min_length=1)
    content_pillars: list[str] = Field(min_length=1, max_length=_MAX_PILLARS)
    tone: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    posting_cadence: str = Field(min_length=1)
    # Structured post frequency (1-7 per week). Optional so legacy personas that
    # predate this field validate cleanly; resolve_posts_per_week() derives the
    # effective value with a regex fallback on posting_cadence prose.
    posts_per_week: int | None = Field(default=None, ge=1, le=7)
    sample_topics: list[str] = Field(default_factory=list, max_length=_MAX_TOPICS)
    # The AI's short "why this lane fits you + why it works on TikTok", surfaced
    # read-only in the dashboard. Optional so a user edit that drops it never
    # fails validation; the generator's prompt reliably fills it (structural-checked).
    rationale: str = ""
    # The single most revealing thing the creator said in the chat interview —
    # shown verbatim as "You said: '...'" on the persona reveal (aha moment).
    # Empty for personas generated from the old flat-field questionnaire.
    signature_quote: str = ""
    # What this page is in service of, in the creator's own terms (e.g. "grow
    # Nova's TikTok audience", "share my pottery"). "" for legacy personas.
    goal: str = ""
    # Where the content comes from. Optional so legacy personas validate;
    # resolve_content_mode() derives the effective value (default create_new —
    # today's de-facto planner behavior).
    content_mode: Literal["existing_footage", "create_new", "mixed"] | None = None
    # The creator's CURRENT situation/location in one line ("based in Istanbul;
    # the Argentina footage is from a past trip"). The planner anchors
    # new-filming ideas ONLY here — never in places that exist only as past
    # footage. "" = unknown → planner writes location-neutral ideas.
    current_situation: str = ""

    def to_dict(self) -> dict:
        return self.model_dump()


def resolve_content_mode(persona: Persona) -> str:
    """Effective content mode for this persona.

    Default "create_new" — the planner's de-facto behavior before the field
    existed (shot lists written as filming instructions), so every legacy
    persona keeps today's output shape.
    """
    return persona.content_mode or "create_new"


def resolve_posts_per_week(persona: Persona) -> int:
    """Derive the effective posts-per-week for this persona.

    Priority:
    1. Structured field (set by the persona LLM or the user directly).
    2. Regex: find the largest integer in 1..7 in the posting_cadence prose
       (handles "3-4 posts/week" → 4, "post 3x a week" → 3).
    3. Fallback: 7 — preserves the pre-feature behavior of one item per day.
    """
    if persona.posts_per_week is not None:
        return max(1, min(7, persona.posts_per_week))
    numbers = [int(n) for n in re.findall(r"\d+", persona.posting_cadence) if 1 <= int(n) <= 7]
    if numbers:
        return max(numbers)
    return 7
