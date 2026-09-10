"""Replay/live eval gate for nova.creator.main."""

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "main_creator"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda path: path.stem)
def test_main_creator_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    judge = judge_for(fixture.agent) if with_judge else None
    result = run_eval(
        fixture,
        model_client=live_model_client if eval_mode == "live" else None,
        judge=judge,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )
    assert result.passed, f"{result.summary()}: {result.structural_failures}"

    expected = fixture.meta.get("text_intent")
    if expected:
        from app.agents._schemas.creator_agent import (
            CreativeStrategy,
            CreatorRenderIntentEvidence,
        )
        from app.routes.creator_agent import _apply_explicit_render_intent

        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        assert strategy["opening_title"] == expected["title"]
        assert strategy["text_color"] == expected["color"]
        assert strategy["target_duration_s"] <= 12
        evidence = CreatorRenderIntentEvidence.model_validate(action["render_intent_evidence"])
        validated = _apply_explicit_render_intent(
            CreativeStrategy.model_validate(strategy),
            fixture.input["creator_request"],
            render_intent_evidence=evidence,
        )
        assert validated.opening_title == expected["title"]
        assert validated.text_color == expected["color"]
