"""Per-fixture eval gate for nova.layout.transition_picker.

The picker's quality is invisible in unit tests — every valid pick passes
the Pydantic Literal, but a whip-pan between two static shots reads as a
glitch and a hard-cut at duration=0.8 violates the "instant" contract. The
structural floor catches both, and the judge rubric scores Rule 0 default
fidelity + camera-movement compatibility + pacing modulation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "transition_picker"
AGENT_NAME = "nova.layout.transition_picker"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=(
        f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/ — "
        "hand-author golden fixtures or capture prod transition logs first"
    ),
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_transition_picker_eval(
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
