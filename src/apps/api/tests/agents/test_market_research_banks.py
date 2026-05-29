"""Unit tests for the TikTok market-research artifact banks (no network, no DB).

Covers the loaders, schema validation, prompt injection into the plan agents,
and the version-coupling guard: because persona_archetypes.json /
content_ideas.json / overlay_examples.json are PART of their agents' prompts,
their `version` must move in lockstep with the agent prompt_version (the same
"stale cache in prod" trap CLAUDE.md calls out for overlay + Layer-2).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.agents._schemas.content_plan import (
    CONTENT_PLAN_PROMPT_VERSION,
    ContentPlanInput,
)
from app.agents._schemas.market_research import ContentIdea, PersonaArchetype
from app.agents._schemas.persona import PERSONA_PROMPT_VERSION, Persona, PersonaQuestionnaire
from app.agents.content_plan_generator import ContentPlanGeneratorAgent
from app.agents.overlay_examples import library_version, load_overlay_examples
from app.agents.persona_examples import (
    archetypes_version,
    content_ideas_version,
    format_archetypes,
    format_ideas_for_pillars,
    load_content_ideas,
    load_persona_archetypes,
)
from app.agents.persona_generator import PersonaGeneratorAgent

_PROMPTS = Path(__file__).resolve().parent.parent.parent / "prompts"


# ---- loaders + schema -------------------------------------------------------


def test_archetype_bank_loads_and_validates():
    archetypes = load_persona_archetypes()
    assert archetypes and all(isinstance(a, PersonaArchetype) for a in archetypes)
    # No @handles / brand names leaking into reference bodies — provenance lives
    # in `source` only.
    for a in archetypes:
        assert "@" not in a.summary and "@" not in a.tone
        for h in a.sample_hooks:
            assert "@" not in h


def test_idea_bank_loads_and_validates():
    ideas = load_content_ideas()
    assert ideas and all(isinstance(i, ContentIdea) for i in ideas)
    for i in ideas:
        assert "@" not in i.idea and "@" not in i.hook_pattern


def test_overlay_examples_still_valid_with_optional_provenance():
    # Adding optional niche/source must not break the hand-written entries.
    examples = load_overlay_examples()
    assert examples
    mined = [e for e in examples if e.source]
    assert mined, "expected at least one mined overlay example with provenance"


# ---- prompt injection -------------------------------------------------------


def test_persona_prompt_injects_archetypes():
    agent = PersonaGeneratorAgent(MagicMock())
    txt = agent.render_prompt(PersonaQuestionnaire(work="barista", hobbies="powerlifting"))
    assert "$archetypes" not in txt  # fully substituted
    assert "STYLE_REFERENCES" in txt
    # The reference bank actually made it in.
    assert load_persona_archetypes()[0].niche in txt


def test_content_plan_prompt_injects_idea_bank_ranked_by_pillars():
    agent = ContentPlanGeneratorAgent(MagicMock())
    persona = Persona(
        summary="you document solo adventures",
        content_pillars=["solo travel", "fitness milestones"],
        tone="brave",
        audience="beginners",
        posting_cadence="3/wk",
        sample_topics=["a run"],
    )
    txt = agent.render_prompt(ContentPlanInput(persona=persona, events="", horizon_days=30))
    assert "$idea_bank" not in txt
    assert "IDEA_BANK" in txt
    # Pillar-ranked: a solo-travel idea should appear before a founder-launch one.
    assert "solo travel & outdoors" in txt


def test_format_ideas_falls_back_when_no_pillar_matches():
    # Unknown pillars still yield the top-N bank, never an empty block.
    out = format_ideas_for_pillars(["quantum knitting"], limit=5)
    assert out != "(none)"
    assert out.count("\n") == 4  # 5 lines


def test_format_archetypes_respects_limit():
    assert format_archetypes(limit=1).count("\n") == 0  # single line, no newline


# ---- version-coupling guard -------------------------------------------------
# These are the contract: the bank's `version` and the consuming agent's
# prompt_version were both touched in the same change. We assert they MATCH the
# committed constants so a future bank edit that forgets the agent bump fails CI.


def test_persona_bank_version_couples_to_prompt_version():
    assert archetypes_version() == "2026-05-29"
    assert PERSONA_PROMPT_VERSION == "2026-05-29.1"


def test_content_idea_bank_version_couples_to_prompt_version():
    assert content_ideas_version() == "2026-05-29"
    assert CONTENT_PLAN_PROMPT_VERSION == "2026-05-29.1"


def test_overlay_bank_version_couples_to_agent_versions():
    from app.agents.intro_writer import IntroTextWriterAgent
    from app.agents.overlay_format_matcher import OverlayFormatMatcherAgent

    # Each is an independent committed-constant tripwire: a bank edit must bump
    # `library_version()` (and re-trip this guard), and a consuming-agent prompt
    # change must bump that agent's prompt_version. They need not be equal — a
    # prompt-template change can lead the bank version. intro_writer's prompt
    # gained the $persona_context block in 2026-05-30 while the overlay_examples
    # bank itself is unchanged from 2026-05-29.
    assert library_version() == "2026-05-29"
    assert IntroTextWriterAgent.spec.prompt_version == "2026-05-30"
    assert OverlayFormatMatcherAgent.spec.prompt_version == "2026-05-29"


@pytest.mark.parametrize("name", ["persona_archetypes", "content_ideas", "overlay_examples"])
def test_bank_files_are_valid_json_with_version(name):
    data = json.loads((_PROMPTS / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(data.get("version"), str) and data["version"]
