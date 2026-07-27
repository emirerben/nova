"""English and Turkish replay/live quality gate for nova.edit.director."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents._runtime import ModelClient, ModelInvocation
from app.config import settings

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "edit_director"
AGENT_NAME = "nova.edit.director"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


class _ModelOverrideClient(ModelClient):
    """Live-eval adapter: hold prompt/input fixed and swap only the Gemini SKU."""

    def __init__(self, delegate: ModelClient, model: str) -> None:
        self.delegate = delegate
        self.model = model

    def invoke(self, **kwargs) -> ModelInvocation:
        return self.delegate.invoke(**{**kwargs, "model": self.model})


@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_edit_director_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    assert fixture.agent == AGENT_NAME
    result = run_eval(
        fixture,
        model_client=live_model_client if eval_mode == "live" else None,
        judge=judge_for(fixture.agent) if with_judge else None,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )
    assert result.passed, (
        f"\n{result.fixture_id}: {result.summary()}\n"
        f"  failures: {result.structural_failures}\n"
        f"  error: {result.error}"
    )


def test_live_pro_outperforms_flash_on_editorial_quality(
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
) -> None:
    """Launch gate: same fixtures and rubric, Pro average must beat Flash."""
    if eval_mode != "live" or not with_judge or live_model_client is None:
        pytest.skip("requires --eval-mode=live --with-judge")

    judge = judge_for(AGENT_NAME)
    pro_scores: list[float] = []
    flash_scores: list[float] = []
    flash_client = _ModelOverrideClient(
        live_model_client,
        settings.edit_director_fallback_model,
    )
    for path in FIXTURE_PATHS:
        fixture = load_fixture(path)
        pro = run_eval(fixture, model_client=live_model_client, judge=judge)
        flash = run_eval(fixture, model_client=flash_client, judge=judge)
        assert pro.passed, f"Pro failed {fixture.fixture_id}: {pro.summary()}"
        assert not flash.structural_failures and flash.error is None, (
            f"Flash failed structurally for {fixture.fixture_id}: {flash.summary()}"
        )
        assert pro.judge is not None and flash.judge is not None
        pro_scores.append(pro.judge.avg)
        flash_scores.append(flash.judge.avg)

    pro_avg = sum(pro_scores) / len(pro_scores)
    flash_avg = sum(flash_scores) / len(flash_scores)
    assert pro_avg >= 4.0
    assert pro_avg > flash_avg, f"Pro {pro_avg:.2f} did not beat Flash {flash_avg:.2f}"
