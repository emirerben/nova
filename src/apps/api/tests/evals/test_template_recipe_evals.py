"""Per-fixture eval gate for nova.compose.template_recipe.

Run modes (see tests/evals/README.md for full guide):
  pytest tests/evals/test_template_recipe_evals.py -v
  pytest tests/evals/test_template_recipe_evals.py -v --with-judge
  pytest tests/evals/test_template_recipe_evals.py -v --eval-mode=live --with-judge
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "template_recipe"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=(
        f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/ — "
        "run `python scripts/export_eval_fixtures.py` first"
    ),
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_template_recipe_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    if fixture.agent != "nova.compose.template_recipe":
        pytest.skip(f"fixture is for {fixture.agent}, not template_recipe")

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
