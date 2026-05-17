"""Per-fixture eval gate for nova.compose.template_text.

Run modes (see tests/evals/README.md for full guide):
  pytest tests/evals/test_template_text_evals.py -v
  pytest tests/evals/test_template_text_evals.py -v --with-judge
  pytest tests/evals/test_template_text_evals.py -v --eval-mode=live --with-judge

Ground truth: if a file at `tests/fixtures/agent_evals/template_text/ground_truth/<slug>.json`
exists, the eval scores predicted overlays against it (completeness, IoU,
color match) and passes those numbers to the judge. Without ground truth the
judge falls back to qualitative inspection per the rubric's fallback clause.

Build ground truth with `scripts/build_text_ground_truth.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents._runtime import RunContext

from .runners.eval_runner import (
    CassetteModelClient,
    EvalResult,
    discover_fixtures,
    load_fixture,
)
from .runners.structural import run_structural
from .runners.text_overlay_scoring import OverlayScores, score_overlays

AGENT_DIR = "template_text"
AGENT_NAME = "nova.compose.template_text"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)

GROUND_TRUTH_DIR = (
    Path(__file__).parent.parent / "fixtures" / "agent_evals" / AGENT_DIR / "ground_truth"
)


def _load_ground_truth(fixture_path: Path) -> list[dict] | None:
    """Look for a matching ground-truth file by stem.

    `prod_snapshots/foo.json` → `ground_truth/foo.json`. Returns the list of
    overlay dicts or None when no ground truth exists. None means the eval
    runs in qualitative-only mode.
    """
    candidate = GROUND_TRUTH_DIR / f"{fixture_path.stem}.json"
    if not candidate.exists():
        return None
    data = json.loads(candidate.read_text())
    overlays = data.get("overlays")
    if not isinstance(overlays, list):
        raise ValueError(f"ground truth {candidate} missing 'overlays' list")
    return overlays


def _summarize_scoring(s: OverlayScores) -> str:
    return (
        f"completeness={s.completeness:.2f} "
        f"precision={s.precision:.2f} "
        f"t_iou={s.mean_temporal_iou:.2f} "
        f"s_iou={s.mean_spatial_iou:.2f} "
        f"colors={s.color_match_fraction:.2f} "
        f"effects={s.effect_label_accuracy:.2f} "
        f"(matched {s.matched_count}/{s.truth_count}, pred={s.predicted_count})"
    )


# Hard floors on the ground-truth scoring numbers. These are deliberately
# loose for the FIRST iteration of the agent — they exist so a catastrophic
# regression (agent suddenly returns no overlays, returns wrong text, etc.)
# fails the eval, but don't gate routine prompt-iteration drift. Tighten over
# time as the agent improves.
COMPLETENESS_HARD_FLOOR = 0.50
TEMPORAL_IOU_HARD_FLOOR = 0.30
SPATIAL_IOU_HARD_FLOOR = 0.20


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=(
        f"no fixtures under tests/fixtures/agent_evals/{AGENT_DIR}/ — "
        "run `python scripts/export_eval_fixtures.py --only template_text` first"
    ),
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_template_text_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
) -> None:
    if "ground_truth" in fixture_path.parts:
        pytest.skip("ground-truth files are eval inputs, not standalone fixtures")

    fixture = load_fixture(fixture_path)
    if fixture.agent != AGENT_NAME:
        pytest.skip(f"fixture is for {fixture.agent}, not {AGENT_NAME}")

    # Build the client + agent (mirrors run_eval but with custom judge payload).
    from app.agents.template_text import TemplateTextAgent

    client = live_model_client if eval_mode == "live" else CassetteModelClient(fixture.raw_text)
    agent = TemplateTextAgent(client)
    eval_ctx = RunContext(
        extra={"skip_langfuse_trace": True, "skip_agent_run_persist": True}
    )

    effective_input = fixture.input
    if live_input_normalizer is not None and eval_mode == "live":
        try:
            effective_input = live_input_normalizer(fixture.input)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"live input normalization failed: {exc}")

    try:
        output = agent.run(effective_input, ctx=eval_ctx)
    except Exception as exc:
        pytest.fail(f"agent.run failed: {exc}")

    validated_input = agent.Input.model_validate(effective_input)
    structural_failures = run_structural(AGENT_NAME, output, validated_input)

    # Ground-truth scoring (objective, deterministic).
    truth = _load_ground_truth(fixture_path)
    scoring: OverlayScores | None = None
    floor_failures: list[str] = []
    if truth is not None:
        predicted = [ov.model_dump() for ov in output.overlays]
        scoring = score_overlays(predicted, truth)
        # Apply hard floors — only gate if ground truth was available.
        if scoring.completeness < COMPLETENESS_HARD_FLOOR:
            floor_failures.append(
                f"completeness {scoring.completeness:.2f} < hard floor "
                f"{COMPLETENESS_HARD_FLOOR}"
            )
        # Temporal/spatial IoU floors only fire when we actually matched at
        # least one pair — otherwise the mean is zero by convention and the
        # completeness floor already caught the regression.
        if scoring.matched_count > 0:
            if scoring.mean_temporal_iou < TEMPORAL_IOU_HARD_FLOOR:
                floor_failures.append(
                    f"mean_temporal_iou {scoring.mean_temporal_iou:.2f} < hard floor "
                    f"{TEMPORAL_IOU_HARD_FLOOR}"
                )
            if scoring.mean_spatial_iou < SPATIAL_IOU_HARD_FLOOR:
                floor_failures.append(
                    f"mean_spatial_iou {scoring.mean_spatial_iou:.2f} < hard floor "
                    f"{SPATIAL_IOU_HARD_FLOOR}"
                )

    all_structural = structural_failures + floor_failures

    judge_result = None
    if with_judge and not all_structural:
        judge = judge_for(fixture.agent)
        # Augment the output payload with scoring numbers so the judge can
        # anchor on them per the rubric's "Anchor on scoring.X if present"
        # instructions.
        payload = output.model_dump()
        if scoring is not None:
            payload["scoring"] = scoring.to_judge_dict()
            payload["ground_truth_overlay_count"] = len(truth) if truth else 0
        else:
            payload["scoring"] = None
        judge_result = judge.score(
            agent_name=fixture.agent,
            agent_input=fixture.input,
            agent_output=payload,
        )

    result = EvalResult(
        fixture_id=fixture.fixture_id,
        agent=fixture.agent,
        prompt_version=fixture.prompt_version,
        structural_failures=all_structural,
        judge=judge_result,
    )

    detail = ""
    if scoring is not None:
        detail = f"\n  scoring: {_summarize_scoring(scoring)}"

    assert result.passed, (
        f"\n{result.fixture_id}: {result.summary()}{detail}\n"
        f"  failures: {result.structural_failures}\n"
        f"  error: {result.error}"
    )
