"""Smoke test: every registered agent imports cleanly and conforms to the runtime."""

from __future__ import annotations

from app.agents._registry import AGENTS, get_agent
from app.agents._runtime import Agent, AgentSpec


_EXPECTED = {
    "nova.video.clip_metadata",
    "nova.compose.template_recipe",
    "nova.audio.transcript",
    "nova.audio.template_recipe",
    "nova.compose.platform_copy",
    "nova.compose.creative_direction",
    "nova.layout.text_designer",
    "nova.video.shot_ranker",
    "nova.layout.transition_picker",
    "nova.audio.beat_aligner",
    "nova.video.clip_router",
    "nova.qa.output_validator",
}


def test_all_twelve_agents_register() -> None:
    registered = AGENTS()
    assert set(registered.keys()) == _EXPECTED, (
        f"missing: {_EXPECTED - set(registered.keys())}, "
        f"extra: {set(registered.keys()) - _EXPECTED}"
    )


def test_each_agent_class_is_well_formed() -> None:
    for name, cls in AGENTS().items():
        assert issubclass(cls, Agent), f"{name}: not an Agent subclass"
        assert isinstance(cls.spec, AgentSpec), f"{name}: missing spec"
        assert cls.spec.name == name, f"{name}: spec.name mismatch ({cls.spec.name!r})"
        assert cls.Input is not None, f"{name}: missing Input"
        assert cls.Output is not None, f"{name}: missing Output"


def test_get_agent_resolves_each() -> None:
    for name in _EXPECTED:
        cls = get_agent(name)
        assert cls.spec.name == name
