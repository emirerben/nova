"""A JSON guard is an explicit transport experiment, never silent repair."""

from dataclasses import asdict

import pytest

from app.agents.main_creator import MainCreatorAgent
from app.services.creator_model_comparison import Arm, build_agent, prepare_case


@pytest.mark.parametrize("agent_name", ["main_creator", "edit_copilot"])
def test_guard_is_explicit_versioned_and_does_not_change_legacy(agent_name):
    import json
    from pathlib import Path

    fixture = json.loads(
        next(
            (Path(__file__).parents[1] / "fixtures/agent_evals" / agent_name).rglob("*.json")
        ).read_text()
    )
    before = asdict(MainCreatorAgent.spec)
    original = build_agent(agent_name, Arm("S", "claude-sonnet-5-5", "high"), None)
    guarded = build_agent(agent_name, Arm("SG", "claude-sonnet-5-5", "high", "json_guarded"), None)
    inp = original.Input.model_validate(fixture["input"])
    prompt = original.render_prompt(inp)
    assert guarded.render_prompt(inp).startswith(prompt)
    assert "Do not emit function-call markup" in guarded.render_prompt(inp)
    assert "Do not emit function-call markup" not in prompt
    assert guarded.spec.prompt_version == original.spec.prompt_version + "+json-guard-v1"
    assert guarded.spec.response_schema is None
    assert guarded.spec.max_attempts == 1 and not guarded.spec.fallback_models
    assert asdict(MainCreatorAgent.spec) == before
    case = {
        "id": "case",
        "agent": agent_name,
        "input": fixture["input"],
        "source_kind": "synthetic_control",
        "split": "development",
    }
    legacy_binding = prepare_case(case, Arm("S", "claude-sonnet-5-5", "high"))
    guard_binding = prepare_case(case, Arm("SG", "claude-sonnet-5-5", "high", "json_guarded"))
    assert guard_binding["output_mode"] == "prompt_json_guarded"
    assert guard_binding["prompt_sha256"] != legacy_binding["prompt_sha256"]
    assert guard_binding["estimated_max_usd"] > legacy_binding["estimated_max_usd"]


def test_guard_does_not_grant_native_sonnet_compatibility():
    with pytest.raises(ValueError, match="compiled grammar"):
        build_agent("edit_copilot", Arm("S", "claude-sonnet-5-5", "high", "native"), None)


@pytest.mark.parametrize("agent_name", ["main_creator", "edit_copilot"])
def test_server_configuration_preserves_selected_guard_transport(agent_name, monkeypatch):
    from app.config import settings
    from app.services.creator_model_comparison import agent_class, configure_creator_agent

    monkeypatch.setattr(settings, agent_name + "_output_mode", "json_guarded")
    original = agent_class(agent_name)(None)
    configured = configure_creator_agent(original, agent_name)
    assert configured.spec.response_schema is None
    assert configured.spec.prompt_version == original.spec.prompt_version + "+json-guard-v1"
    assert not hasattr(configured, "wire_contract")
