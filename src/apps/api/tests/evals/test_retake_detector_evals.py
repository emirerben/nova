"""Per-fixture eval gate for nova.audio.retake_detector (plans/010 T7).

Structural-only in CI (replay mode, no network). Live mode requires
GEMINI_API_KEY; the judge additionally requires ANTHROPIC_API_KEY.

Fixture coverage (all hand-authored golden — retake spans are never persisted,
so there are no prod snapshots to export):
  - EN restart ("wait, let me start over") — abandoned span flagged
  - TR restart ("dur baştan alayım") — abandoned span flagged
  - rhetorical-repetition near-miss NEGATIVE — must return NO retakes
  - clean transcript NEGATIVE — must return NO retakes
  - empty transcript NEGATIVE — must return NO retakes

Run modes (see tests/evals/README.md):
  pytest tests/evals/test_retake_detector_evals.py -v
  NOVA_EVAL_MODE=live pytest tests/evals/test_retake_detector_evals.py -v --eval-mode=live
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents._runtime import RunContext
from app.agents.retake_detector import RetakeDetectorAgent

from .runners.eval_runner import CassetteModelClient, discover_fixtures, load_fixture, run_eval

AGENT_DIR = "retake_detector"
AGENT_NAME = "nova.audio.retake_detector"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=(
        f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/ — "
        "add hand-authored golden fixtures"
    ),
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_retake_detector_eval(
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


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/",
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_negative_fixtures_return_no_retakes(
    fixture_path: Path,
    eval_mode: str,
    live_model_client,
) -> None:
    """Negatives (meta.expect_empty) MUST produce an empty retake list.

    This is the false-positive gate the plan calls out: rhetorical repetition
    and clean transcripts must never be flagged. In replay mode it pins the
    recorded cassette + parse() tolerance; in live mode it actually gates the
    prompt's false-positive discipline against real Gemini.
    """
    fixture = load_fixture(fixture_path)
    if fixture.agent != AGENT_NAME:
        pytest.skip(f"fixture is for {fixture.agent}, not {AGENT_NAME}")
    if not fixture.meta.get("expect_empty"):
        pytest.skip("positive fixture — empty-output assertion not applicable")

    client = live_model_client if eval_mode == "live" else CassetteModelClient(fixture.raw_text)
    agent = RetakeDetectorAgent(client)
    output = agent.run(
        fixture.input,
        ctx=RunContext(extra={"skip_langfuse_trace": True, "skip_agent_run_persist": True}),
    )

    assert output.retakes == [], (
        f"{fixture.fixture_id}: negative fixture flagged "
        f"{len(output.retakes)} retake span(s): "
        f"{[s.model_dump() for s in output.retakes]}"
    )
