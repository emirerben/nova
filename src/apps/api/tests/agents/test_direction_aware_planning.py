"""Guards for direction-aware planning (P1 interview fork + P7 location grounding).

Pins:
  - Legacy personas (no goal/content_mode/current_situation) validate and
    resolve to create_new — zero-migration contract.
  - goal/current_situation reach the planner prompt; absent → near-baseline.
  - content-mode directive blocks render per mode; create_new stays "".
  - The past-trips-are-edit-material rule is ALWAYS in the prompt (the
    Buenos Aires incident class).
"""

from __future__ import annotations

from app.agents._schemas.content_plan import ContentPlanInput
from app.agents._schemas.persona import Persona, resolve_content_mode
from app.agents.content_plan_generator import ContentPlanGeneratorAgent


def _persona(**overrides) -> Persona:
    base = dict(
        summary="You document life between Istanbul and the road.",
        content_pillars=["street food runs", "ferry commutes", "studio days"],
        tone="warm and dry",
        audience="people planning a move abroad",
        posting_cadence="3-4 posts/week",
        posts_per_week=4,
        sample_topics=["ferry sunrise", "the corner simit stand"],
    )
    base.update(overrides)
    return Persona(**base)


def _render(persona: Persona) -> str:
    agent = ContentPlanGeneratorAgent.__new__(ContentPlanGeneratorAgent)
    return agent.render_prompt(ContentPlanInput(persona=persona, horizon_days=30))


class TestLegacyPersonaContract:
    def test_persona_schema_defaults_content_mode_create_new(self):
        p = _persona()  # no goal / content_mode / current_situation
        assert p.goal == ""
        assert p.content_mode is None
        assert p.current_situation == ""
        assert resolve_content_mode(p) == "create_new"

    def test_legacy_persona_dict_roundtrip(self):
        # Stored JSONB rows predate the fields entirely.
        p = Persona(**_persona().model_dump(exclude={"goal", "content_mode", "current_situation"}))
        assert resolve_content_mode(p) == "create_new"


class TestPromptDirection:
    def test_content_plan_prompt_includes_current_situation(self):
        p = _persona(
            goal="grow Nova's TikTok audience",
            current_situation="based in Istanbul; the Argentina footage is a past trip",
        )
        prompt = _render(p)
        assert "goal: grow Nova's TikTok audience" in prompt
        assert "current situation: based in Istanbul" in prompt

    def test_legacy_persona_renders_no_direction_lines(self):
        prompt = _render(_persona())
        assert "\ngoal: " not in prompt
        assert "current situation:" not in prompt

    def test_past_trip_rule_always_present(self):
        # Static quality rule — must hold for legacy and new personas alike.
        for p in (_persona(), _persona(content_mode="existing_footage")):
            prompt = _render(p)
            assert "Past trips are EDIT material" in prompt

    def test_existing_footage_mode_block(self):
        prompt = _render(_persona(content_mode="existing_footage"))
        assert "CONTENT MODE — EXISTING FOOTAGE" in prompt
        assert "Never ask them to go film" in prompt

    def test_mixed_mode_block(self):
        prompt = _render(_persona(content_mode="mixed"))
        assert "CONTENT MODE — MIXED" in prompt

    def test_create_new_mode_is_baseline(self):
        prompt = _render(_persona(content_mode="create_new"))
        assert "CONTENT MODE" not in prompt
