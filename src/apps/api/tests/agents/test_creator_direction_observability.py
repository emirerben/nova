from app.agents._runtime import AgentSpec, _project_sensitive_input
from app.agents.content_plan_generator import ContentPlanGeneratorAgent
from app.agents.main_creator import MainCreatorAgent


def test_creator_direction_is_redacted_from_agent_run_input():
    projected = _project_sensitive_input(
        {"creator_direction": "- Always use Playfair Display", "items": [{"x": 1}]}
    )

    assert projected["creator_direction"]["redacted"] is True
    assert "Playfair" not in str(projected)
    assert projected["items"] == [{"x": 1}]


def test_direction_consuming_planners_suppress_raw_io_and_output_text():
    assert MainCreatorAgent.spec.sensitive_io is True
    assert ContentPlanGeneratorAgent.spec.sensitive_io is True

    planner = ContentPlanGeneratorAgent(object())
    projected = planner.project_output_for_observability(
        {"items": [{"idea": "Always use Playfair Display"}]}
    )

    assert projected is not None
    assert projected["redacted"] is True
    assert "Playfair" not in str(projected)
    assert (
        AgentSpec(name="plain", prompt_id="plain", prompt_version="1", model="m").sensitive_io
        is False
    )
