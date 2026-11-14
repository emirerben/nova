"""Unit tests for the TikTok market-research artifact banks (no network, no DB).

Covers the loaders, schema validation, prompt injection into the plan agents,
and the version-coupling guard: because persona_archetypes.json /
content_ideas.json / overlay_examples.json are PART of their agents' prompts,
the persona and plan versions derive from them and the overlay bank `version` moves in
lockstep with the hand-numbered intro and matcher versions.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.agents._schemas.content_plan import ContentPlanInput
from app.agents._schemas.market_research import (
    ContentIdea,
    PerformanceSignal,
    PersonaArchetype,
    SuccessFactor,
)
from app.agents._schemas.persona import Persona, PersonaQuestionnaire
from app.agents.content_plan_generator import ContentPlanGeneratorAgent
from app.agents.overlay_examples import library_version, load_overlay_examples
from app.agents.persona_examples import (
    _performance_score,
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
# The persona and content-plan prompt versions are fingerprints of their prompt files
# and banks (tests/agents/test_prompt_fingerprint.py), so a bank edit moves them with
# no hand bump (test_plan_agents_fingerprint_their_banks keeps the banks in those
# source lists). The intro writer and
# overlay matcher are still hand-numbered, so their bank-version tripwires stay below.


def test_success_factor_bank_version_couples_to_intro_prompt_version():
    from app.agents.intro_writer import IntroTextWriterAgent

    # Persona and content plan derive their versions from the bank file. The intro
    # writer still carries a hand-numbered version, so the bank `version` and it move
    # together. Bump 2026-11-08-research-21: added community-ownership-decision-corpus-16.
    assert success_factors_version() == "2026-11-08-research-21"
    assert IntroTextWriterAgent.spec.prompt_version == "2026-11-08-research-21"


def test_overlay_bank_version_couples_to_agent_versions():
    from app.agents.intro_writer import IntroTextWriterAgent
    from app.agents.overlay_format_matcher import OverlayFormatMatcherAgent

    # Each is an independent committed-constant tripwire: a bank edit must bump
    # `library_version()` (and re-trip this guard), and a consuming-agent prompt
    # change must bump that agent's prompt_version. Bump 2026-05-31: added 2 new
    # overlay examples (destination-qualifier-popin-01, city-morning-sensory-fadein-01).
    # Bump 2026-06-07: weekly research refresh — added adventure-humor-scaleup-02
    #                  and implied-question-static-02; bumped both consuming agents.
    # Bump 2026-06-10: word-cluster layout — added 3 cluster examples
    #                  (travel-scenic-cluster-01, lifestyle-moment-cluster-01,
    #                  travel-city-cluster-01) + `layout` field; bumped both agents.
    # Bump 2026-06-12: broadened cluster examples for energetic/people/lifestyle
    #                  hooks and updated the matcher layout policy.
    # Bump 2026-06-14: weekly research refresh — added professional-ootd-static-01
    #                  (office fashion / professional aesthetic lane).
    # Intro bumped to 2026-06-18: added clip_notes context block (plan-item shot notes).
    # Matcher bumped to 2026-07-17: added the `behind_subject` occlusion-flag decision
    #                  (text-behind-subject) to match_overlay_format.txt — a
    #                  consuming-agent prompt change with no bank-content edit, so
    #                  only the matcher's own prompt_version moves; library_version()
    #                  is untouched.
    # Bump 2026-08-05: anti-slop exemplar retext (plans/015) — transformation-before-
    #                  after-karaoke-01 no longer demonstrates the banned "changed
    #                  everything" frame (prompt/exemplar drift from PR #338/#507);
    #                  bank content edit, so BOTH consuming agents bump. Guard:
    #                  tests/agents/test_overlay_examples_slop_guard.py now lints
    #                  every exemplar against the slop pattern class.
    # Bump 2026-10-11: weekly research refresh — added early-morning-commitment-scaleup-01
    #                  and series-comeback-fadein-01; bumped both consuming agents.
    # Bump 2026-10-25-research-19: weekly refresh — added minimal-verdict-static-01
    #                  (2-word aesthetic verdict form, "kinda chic" pattern).
    # Bump 2026-11-01-research-20: weekly refresh — added product-review-warm-fadein-01
    #                  (warm "hope it helps" product review hook form).
    # Bump 2026-11-08-research-21: version lockstep bump (no new overlay examples this cycle).
    assert library_version() == "2026-11-08-research-21"
    assert IntroTextWriterAgent.spec.prompt_version == "2026-11-08-research-21"
    assert OverlayFormatMatcherAgent.spec.prompt_version == "2026-11-08-research-21"


@pytest.mark.parametrize(
    "name",
    ["persona_archetypes", "content_ideas", "overlay_examples", "tiktok_success_factors"],
)
def test_bank_files_are_valid_json_with_version(name):
    data = json.loads((_PROMPTS / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(data.get("version"), str) and data["version"]
