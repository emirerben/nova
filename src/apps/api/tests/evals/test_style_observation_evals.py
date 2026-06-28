"""Per-fixture eval gate for nova.video.style_observation.

NOTE: This is a VISION eval — the agent watches actual video files uploaded to
the Gemini File API. Fixtures reference a ``file_uri`` that expires after ~48h.
For this reason:
  - Structural-only mode (default): exercises parse() coercion on the stored
    ``raw_text`` field, no Gemini calls, runs in CI.
  - Live mode (--eval-mode=live): re-runs the actual Gemini call with the
    fixture's stored ``file_uri`` — ONLY meaningful if the fixture was captured
    recently (< 48h). Use for PR validation before flip gates.

Run:
    cd src/apps/api
    pytest tests/evals/test_style_observation_evals.py                    # structural
    NOVA_EVAL_MODE=live pytest ... --eval-mode=live --with-judge           # live + judge
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "style_observation"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=(
        f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/ — "
        "capture a fixture first: run the agent against a real video and "
        "save the result as a JSON fixture following the standard format."
    ),
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_style_observation_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    if fixture.agent != "nova.video.style_observation":
        pytest.skip(f"fixture is for {fixture.agent}, not style_observation")

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
