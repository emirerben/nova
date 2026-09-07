"""Replay/live eval gate for the narration annotation grounding agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.narration_annotations import NarrationAnnotationAgent

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "narration_annotations"
AGENT_NAME = "nova.compose.narration_annotations"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/",
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_narration_annotations_eval(
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
    assert fixture.prompt_version == NarrationAnnotationAgent.spec.prompt_version

    result = run_eval(
        fixture,
        model_client=live_model_client if eval_mode == "live" else None,
        judge=judge_for(fixture.agent) if with_judge else None,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )

    assert result.passed, (
        f"\n{result.fixture_id}: {result.summary()}\n"
        f"  failures: {result.structural_failures}\n"
        f"  error: {result.error}"
    )
