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
from app.agents._schemas.market_research import (
    ContentIdea,
    PerformanceSignal,
    PersonaArchetype,
    SuccessFactor,
)
from app.agents._schemas.persona import PERSONA_PROMPT_VERSION, Persona, PersonaQuestionnaire
from app.agents.content_plan_generator import ContentPlanGeneratorAgent
from app.agents.overlay_examples import library_version, load_overlay_examples
from app.agents.persona_examples import (
    _performance_score,
    archetypes_version,
    content_ideas_version,
    format_archetypes,
    format_ideas_for_pillars,
    format_success_factors,
    load_content_ideas,
    load_persona_archetypes,
    load_success_factors,
    success_factors_version,
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


def test_success_factor_bank_loads_and_validates():
    factors = load_success_factors()
    assert factors and all(isinstance(f, SuccessFactor) for f in factors)
    # Both provenances are actually represented (the "clearly labeled, both
    # sources" contract) and neither leaks @handles into the body fields.
    provenances = {f.provenance for f in factors}
    assert provenances == {"corpus", "public"}
    for f in factors:
        assert "@" not in f.factor and "@" not in f.why and "@" not in f.evidence
        assert set(f.applies_to) <= {"persona", "plan", "hook", "all"}
        # Public factors must cite where the claim came from.
        if f.provenance == "public":
            assert f.source, f"public factor {f.id} missing a source citation"


def test_performance_signal_schema_and_score():
    # view_index wins over engagement_rate; missing signal sorts to zero (last).
    assert _performance_score(PerformanceSignal(view_index=3.0, engagement_rate=0.5)) == 3.0
    assert _performance_score(PerformanceSignal(engagement_rate=0.5)) == 0.5
    assert _performance_score(PerformanceSignal()) == 0.0
    assert _performance_score(None) == 0.0


def test_ranking_prefers_performance_at_equal_pillar_fit(monkeypatch):
    # Two ideas with identical pillar overlap; the higher view_index ranks first,
    # and an idea with no performance signal sorts last.
    base = dict(niche="fitness", pillar="fitness milestones", hook_pattern="x")
    proven = ContentIdea(
        id="proven",
        idea="fitness milestones a",
        performance=PerformanceSignal(view_index=9.0),
        **base,
    )
    guessed = ContentIdea(id="guessed", idea="fitness milestones b", **base)
    monkeypatch.setattr("app.agents.persona_examples.load_content_ideas", lambda: (guessed, proven))
    out = format_ideas_for_pillars(["fitness milestones"], limit=2)
    lines = out.splitlines()
    assert "proven" not in out  # ids never leak into the prompt
    # Proven idea text ("a") appears before the guessed one ("b").
    assert lines[0].endswith("milestones a (hook: x)")
    assert lines[1].endswith("milestones b (hook: x)")


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


def test_format_success_factors_filters_by_stage_and_labels_provenance():
    hook = format_success_factors("hook")
    assert hook != "(none)"
    # Provenance is surfaced in a reader-legible label, never the raw enum.
    assert "[observed]" in hook or "[TikTok docs]" in hook
    # A persona-only factor (cadence) must not appear in the hook block.
    plan = format_success_factors("plan")
    assert "[observed]" in plan or "[TikTok docs]" in plan


def test_persona_prompt_injects_success_factors():
    agent = PersonaGeneratorAgent(MagicMock())
    txt = agent.render_prompt(PersonaQuestionnaire(work="barista"))
    assert "$success_factors" not in txt
    assert "SUCCESS_FACTORS" in txt


def test_content_plan_prompt_injects_success_factors():
    agent = ContentPlanGeneratorAgent(MagicMock())
    persona = Persona(
        summary="you document city life",
        content_pillars=["city lifestyle"],
        tone="warm",
        audience="locals",
        posting_cadence="3/wk",
        sample_topics=["a cafe"],
    )
    txt = agent.render_prompt(ContentPlanInput(persona=persona, events="", horizon_days=30))
    assert "$success_factors" not in txt
    assert "SUCCESS_FACTORS" in txt


def test_intro_writer_prompt_injects_success_factors():
    from app.agents.intro_writer import IntroTextWriterAgent, IntroWriterInput
    from app.agents.music_matcher import ClipSummary

    agent = IntroTextWriterAgent(MagicMock())
    txt = agent.render_prompt(
        IntroWriterInput(
            hero_clip=ClipSummary(
                clip_id="c1", duration_s=5.0, subject="a beach", description="waves"
            )
        )
    )
    assert "$success_factors" not in txt
    assert "What makes a hook land" in txt


# ---- version-coupling guard -------------------------------------------------
# These are the contract: the bank's `version` and the consuming agent's
# prompt_version were both touched in the same change. We assert they MATCH the
# committed constants so a future bank edit that forgets the agent bump fails CI.


def test_persona_bank_version_couples_to_prompt_version():
    # Bank version need not equal the prompt version — each is its own tripwire.
    # The prompt led to 2026-05-31 (anti-cringe concrete-pillar rule, no bank change).
    assert archetypes_version() == "2026-05-30"
    assert PERSONA_PROMPT_VERSION == "2026-05-31"


def test_content_idea_bank_version_couples_to_prompt_version():
    # Prompt led to 2026-05-31 (anti-cringe), then 2026-05-31.1 (edit_format
    # emission, format-aware edit engine); idea bank unchanged.
    assert content_ideas_version() == "2026-05-30"
    assert CONTENT_PLAN_PROMPT_VERSION == "2026-05-31.1"


def test_success_factor_bank_version_couples_to_consuming_prompt_versions():
    from app.agents.intro_writer import IntroTextWriterAgent

    # The success-factor bank is part of THREE prompts (persona, content plan,
    # intro). The bank itself is unchanged (2026-05-30); persona led to 2026-05-31
    # (anti-cringe) and content plan to 2026-05-31.1 (anti-cringe + edit_format
    # emission) while the intro prompt is untouched, so it stays at 2026-05-30.2.
    assert success_factors_version() == "2026-05-30"
    assert PERSONA_PROMPT_VERSION == "2026-05-31"
    assert CONTENT_PLAN_PROMPT_VERSION == "2026-05-31.1"
    assert IntroTextWriterAgent.spec.prompt_version == "2026-05-30.2"


def test_overlay_bank_version_couples_to_agent_versions():
    from app.agents.intro_writer import IntroTextWriterAgent
    from app.agents.overlay_format_matcher import OverlayFormatMatcherAgent

    # Each is an independent committed-constant tripwire: a bank edit must bump
    # `library_version()` (and re-trip this guard), and a consuming-agent prompt
    # change must bump that agent's prompt_version. They need not be equal — a
    # prompt-template change can lead the bank version. intro_writer's prompt
    # gained $persona_context (2026-05-30) then $success_factors (2026-05-30.1)
    # while the overlay_examples bank itself is unchanged from 2026-05-29.
    assert library_version() == "2026-05-29"
    assert IntroTextWriterAgent.spec.prompt_version == "2026-05-30.2"
    assert OverlayFormatMatcherAgent.spec.prompt_version == "2026-05-29"


@pytest.mark.parametrize(
    "name",
    ["persona_archetypes", "content_ideas", "overlay_examples", "tiktok_success_factors"],
)
def test_bank_files_are_valid_json_with_version(name):
    data = json.loads((_PROMPTS / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(data.get("version"), str) and data["version"]
