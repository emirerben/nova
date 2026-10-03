"""Per-fixture eval gate for nova.plan.speech_excerpt_planner (KRI-282).

Replay mode is structural-only and runs in CI. Live evals (Gemini spend) are a
manual gate before relying on the prompt; see tests/evals/README.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "speech_excerpt_planner"
AGENT_NAME = "nova.plan.speech_excerpt_planner"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(not FIXTURE_PATHS, reason="no speech_excerpt_planner fixtures")
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_speech_excerpt_planner_eval(
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
    output = result.output or {}
    expect = fixture.meta.get("expect")
    if expect == "no_excerpts":
        assert output.get("wants_speech_excerpts") is False
    elif expect == "question":
        assert output.get("question") and not output.get("sections")
    elif expect == "excerpts":
        assert output.get("wants_speech_excerpts") is True
        assert any(s.get("kind") == "speech" for s in output.get("sections", []))
