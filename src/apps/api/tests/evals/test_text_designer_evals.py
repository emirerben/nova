"""Per-fixture eval gate for nova.layout.text_designer.

text_designer ships in shadow mode against `_LABEL_CONFIG` — it doesn't
get to commit its output today, but its picks need a quality floor so
prompt-iteration evidence isn't anecdotal. The fixtures here pin the
calibration patterns the prompt documents (signature hook treatment on
slot 1, lighter mid-slots, prefix as the quiet lead-in) so the eval
catches drift away from them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "text_designer"
AGENT_NAME = "nova.layout.text_designer"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=(
        f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/ — "
        "hand-author golden fixtures or capture shadow-mode prod outputs first"
    ),
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_text_designer_eval(
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
