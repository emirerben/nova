"""Per-fixture eval gate for nova.plan.style_derivation (Creator Agent M1).

Structural-only in CI (no Skia, no LLM). Live mode requires ANTHROPIC_API_KEY
and NOVA_EVAL_MODE=live. Capture a golden fixture with --eval-mode=live before
merging any prompt change.

Run modes (see tests/evals/README.md):
  pytest tests/evals/test_style_derivation_evals.py -v
  pytest tests/evals/test_style_derivation_evals.py -v --with-judge
  NOVA_EVAL_MODE=live pytest tests/evals/test_style_derivation_evals.py -v \
    --eval-mode=live --with-judge
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "style_derivation"
AGENT_NAME = "nova.plan.style_derivation"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/",
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_style_derivation_eval(
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
