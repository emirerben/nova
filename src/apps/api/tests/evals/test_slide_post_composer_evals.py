"""Per-fixture eval gate for nova.plan.slide_post_composer.

Run modes (see tests/evals/README.md):
  pytest tests/evals/test_slide_post_composer_evals.py -v
  pytest tests/evals/test_slide_post_composer_evals.py -v --with-judge
  pytest tests/evals/test_slide_post_composer_evals.py -v --eval-mode=live --with-judge

Exact id-coverage is already a hard SchemaError inside `parse()` (plans/024) —
the structural check re-asserts that guarantee against the parsed Output so a
regression there fails here too, not just in the agent's own unit tests. The
judge rubric (tests/evals/rubrics/slide_post_composer.md) scores whether the
proposed order/cover/caption reads like a human editor's choice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "slide_post_composer"
AGENT_NAME = "nova.plan.slide_post_composer"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/",
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_slide_post_composer_eval(
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
