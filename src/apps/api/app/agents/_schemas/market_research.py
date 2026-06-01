"""Schemas for the weekly TikTok market-research artifact banks.

These are the typed shapes of the reference data the research agent mines from
public TikTok accounts and Nova's plan agents few-shot off at runtime:

- `PersonaArchetype` -> prompts/persona_archetypes.json  (the "persona pool" /
  "style types" injected into generate_persona.txt)
- `ContentIdea`      -> prompts/content_ideas.json        (the idea bank injected
  into generate_content_plan.txt)
- `SuccessFactor`    -> prompts/tiktok_success_factors.json (the codified "why
  videos perform" levers injected into generate_persona.txt,
  generate_content_plan.txt, and write_intro_text.txt)

All three files are versioned in-repo and loaded via
app/agents/persona_examples.py, mirroring the overlay_examples.json pattern.
Because the loaded content becomes part of the consuming agents' prompts,
bumping any JSON's `version` MUST go with a bump of the matching agent
prompt_version (PERSONA_PROMPT_VERSION / CONTENT_PLAN_PROMPT_VERSION /
IntroTextWriterAgent.spec.prompt_version) — enforced by the coupling guard test.

These bodies are STYLE/strategy references, never content to reproduce verbatim.
`text`/hook fields carry no @handles or brand names; the `source` field holds
attribution only.

`PerformanceSignal` carries the engagement numbers the fetch stage already
pulls (see scripts/research/fetch_tiktok.py) so the runtime ranking can prefer
ideas/archetypes that actually performed, not just ones whose text overlaps a
pillar. All fields optional — legacy entries (and accounts where TikTok blocks
the enrich fetch) carry no signal and degrade to "rank last", never crash.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PerformanceSignal(BaseModel):
    """Account-size-independent engagement signal for a mined pattern.

    `view_index` (views ÷ the source account's median views) is the strongest
    signal — it says "this outperformed its own account by N×" rather than
    rewarding big accounts for being big. `engagement_rate` is the fallback when
    a median isn't available. Both optional and best-effort (TikTok scraping is
    hostile; the enrich fetch may return nothing)."""

    views: int | None = Field(default=None, ge=0)
    # (likes + comments + reposts) / views — account-size independent.
    engagement_rate: float | None = Field(default=None, ge=0)
    # views / source-account median views — outperformance vs the account's baseline.
    view_index: float | None = Field(default=None, ge=0)
    sampled_at: str | None = None  # e.g. "2026-05"


class PersonaArchetype(BaseModel):
    """A recurring creator identity mined from the market — the unit of the
    'persona pool' and 'style types' the persona generator is steered toward."""

    id: str = Field(min_length=1)
    niche: str = Field(min_length=1)
    summary: str = Field(min_length=1)  # who this creator is + the lane they own
    tone: str = Field(min_length=1)  # the on-screen voice / "style type"
    content_pillars: list[str] = Field(min_length=1, max_length=8)
    audience: str = Field(min_length=1)
    sample_hooks: list[str] = Field(default_factory=list, max_length=8)
    source: str | None = None  # attribution only, e.g. "tiktok:@handle (2026-05)"
    performance: PerformanceSignal | None = None  # how this lane performed (best-effort)


class ContentIdea(BaseModel):
    """A reusable, niche-tagged video concept mined from high-performing posts.
    Templated so the content-plan agent can adapt it to a specific creator."""

    id: str = Field(min_length=1)
    niche: str = Field(min_length=1)
    pillar: str = Field(min_length=1)
    idea: str = Field(min_length=1)  # the concept, e.g. "my very curated [city] guide"
    hook_pattern: str = Field(min_length=1)  # the on-screen/spoken opener pattern
    filming_context: str | None = None  # practical shot/setting tip
    source: str | None = None
    performance: PerformanceSignal | None = None  # how the source post performed (best-effort)


class SuccessFactor(BaseModel):
    """A codified lever for why short-form videos perform, injected into the plan
    agents' prompts so persona/plan/hook decisions cite real strategy rather than
    generic LLM instinct.

    `provenance` keeps the two sources honest and never conflated:
    - "corpus" — observed in OUR fetched engagement data (evidence cites the
      pattern, e.g. "indexed 3x account median across N mined videos").
    - "public" — curated from public TikTok Creator docs / published breakdowns
      (evidence states the claim; `source` cites where it came from).
    """

    id: str = Field(min_length=1)
    factor: str = Field(min_length=1)  # the lever, imperative + short
    why: str = Field(min_length=1)  # why it drives watch-time / reach
    # which agent stage(s) this steers: "persona" | "plan" | "hook" | "all"
    applies_to: list[str] = Field(min_length=1, max_length=4)
    provenance: Literal["corpus", "public"]
    evidence: str = Field(min_length=1)  # the observation (corpus) or claim (public)
    source: str | None = None  # citation (public) / attribution (corpus)
