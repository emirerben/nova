"""Per-fixture eval gate for nova.editor.overlay_generation (KRI-1428).

Run modes (see tests/evals/README.md for full guide):
  pytest tests/evals/test_overlay_generation_evals.py -v
  pytest tests/evals/test_overlay_generation_evals.py -v --with-judge
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "overlay_generation"
AGENT_NAME = "nova.editor.overlay_generation"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=(
        f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/ — "
        "add hand-authored golden fixtures"
    ),
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_overlay_generation_eval(
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
    expected = fixture.meta.get("expect_overlays")
    if expected == "none":
        assert output.get("overlays") == [], "this case must ask or refuse, not place an overlay"
        assert output.get("message"), "an empty plan must say why"
    elif expected == "some":
        assert output.get("overlays"), "this case has evidence and must place at least one overlay"
    expected_depicts = fixture.meta.get("expect_depicts")
    if expected_depicts:
        images = [row for row in output.get("overlays", []) if row["kind"] == "image"]
        assert images and all(row["depicts"] == expected_depicts for row in images)
