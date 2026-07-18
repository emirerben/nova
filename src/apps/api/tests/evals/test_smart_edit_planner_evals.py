"""Replay/live eval gate for the Smart Captions semantic planner."""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "smart_edit_planner"
AGENT_NAME = "nova.compose.smart_edit_planner"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(not FIXTURE_PATHS, reason="no Smart Edit planner fixtures")
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda path: path.stem)
def test_smart_edit_planner_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    if fixture.agent != AGENT_NAME:
        pytest.skip(f"fixture is for {fixture.agent}, not {AGENT_NAME}")

    result = run_eval(
        fixture,
        model_client=live_model_client if eval_mode == "live" else None,
        judge=judge_for(fixture.agent) if with_judge else None,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )
    assert result.passed, (
        f"\n{result.fixture_id}: {result.summary()}\n"
        f"  failures: {result.structural_failures}\n  error: {result.error}"
    )
