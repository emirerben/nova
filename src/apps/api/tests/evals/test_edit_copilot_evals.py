"""Replay eval gate for nova.edit.copilot.

Structural-only in CI: no network, no renderer/Skia imports. Live mode follows
the shared eval harness conventions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "edit_copilot"
AGENT_NAME = "nova.edit.copilot"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


def test_story_native_goldens_pin_the_current_prompt_version() -> None:
    """New story operations must remain attributable to the prompt that authored them."""
    from app.agents.edit_copilot import EDIT_COPILOT_PROMPT_VERSION

    story_fixtures = [path for path in FIXTURE_PATHS if path.stem.startswith("story_")]
    assert story_fixtures, "story-native replay goldens are missing"
    assert all(
        load_fixture(path).prompt_version == EDIT_COPILOT_PROMPT_VERSION for path in story_fixtures
    )


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=(
        f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/ — "
        "add hand-authored golden fixtures"
    ),
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_edit_copilot_eval(
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

    judge = judge_for(fixture.agent) if with_judge else None
    client = live_model_client if eval_mode == "live" else None

    result = run_eval(
        fixture,
        model_client=client,
        judge=judge,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )

    assert result.passed, (
        f"\n{result.fixture_id}: {result.summary()}\n"
        f"  failures: {result.structural_failures}\n"
        f"  error: {result.error}"
    )
