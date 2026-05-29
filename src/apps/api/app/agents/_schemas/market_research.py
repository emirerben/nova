"""Schemas for the weekly TikTok market-research artifact banks.

These are the typed shapes of the reference data the research agent mines from
public TikTok accounts and Nova's plan agents few-shot off at runtime:

- `PersonaArchetype` -> prompts/persona_archetypes.json  (the "persona pool" /
  "style types" injected into generate_persona.txt)
- `ContentIdea`      -> prompts/content_ideas.json        (the idea bank injected
  into generate_content_plan.txt)

Both files are versioned in-repo and loaded via app/agents/persona_examples.py,
mirroring the overlay_examples.json pattern. Because the loaded content becomes
part of the persona/content-plan prompts, bumping either JSON's `version` MUST
go with a bump of the matching agent prompt_version (PERSONA_PROMPT_VERSION /
CONTENT_PLAN_PROMPT_VERSION) — enforced by the coupling guard test.

These bodies are STYLE references for voice steering, never content to
reproduce verbatim. `text`/hook fields carry no @handles or brand names; the
`source` field holds attribution only.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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
