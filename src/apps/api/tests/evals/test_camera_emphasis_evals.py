"""Per-fixture eval gate for nova.compose.camera_emphasis (KRI-7 zoom placement).

Structural-only in CI (replay mode, no network): the recorded raw_text is
replayed through the agent's parse() and the structural floor in
runners/structural.py (check_camera_emphasis — grounded candidate indexes, the
caller's effect cap, one "strong" peak, no back-to-back moments). Live mode
requires GEMINI_API_KEY; the judge additionally ANTHROPIC_API_KEY.

The judge is the layer that can say a pick is boring rather than merely legal —
the structural floor cannot tell a payoff line from a greeting.

Run modes (see tests/evals/README.md):
  pytest tests/evals/test_camera_emphasis_evals.py -v
  NOVA_EVAL_MODE=live pytest tests/evals/test_camera_emphasis_evals.py -v --eval-mode=live
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "camera_emphasis"
AGENT_NAME = "nova.compose.camera_emphasis"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=(
        f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/ — "
        "add hand-authored golden fixtures"
    ),
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_camera_emphasis_eval(
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
