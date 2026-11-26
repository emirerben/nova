"""Eval gate for the story sequencer (KRI-1593): replay goldens, and the held-out comparison
against upload order.

Three layers, each saying only what it can:

* Replay goldens (`fixtures/agent_evals/story_sequencer/golden`): HAND-AUTHORED answers through
  the real parse and the structural contract. They prove the contract, not the model.
* The held-out set (`story_sequencer/heldout.json`): clips listed in the order they were added
  and the precedence pairs a story order must satisfy, written by hand. The free checks here
  prove the SET is fit to judge with: its pairs are consistent, and the upload order really
  fails them (a set the baseline already passes could not show the model is better).
* The comparison itself ("the model beats upload order") needs the live model. A paid run
  (`--eval-mode=live`, an approved development-ledger run) records its answers to
  `baselines/story_sequencer/heldout_run.json`; `test_recorded_run_beats_upload_order` then
  re-scores that file on every CI run, and fails when the prompt version has moved since.
  The recorded run is one sample of clean, hand-written cases: it does not measure real, thin
  clip records, and a new prompt needs a new run.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agents._runtime import SchemaError
from app.agents.story_sequencer import StorySequencerAgent

from .runners.eval_runner import (
    RecordingModelClient,
    build_eval_run_context,
    discover_fixtures,
    load_fixture,
    run_eval,
)
from .story_sequencer import scoring

AGENT_DIR = "story_sequencer"
AGENT_NAME = "nova.kria.story_sequencer"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)
HELDOUT = scoring.load_heldout()
CASES = HELDOUT["cases"]


@pytest.mark.skipif(not FIXTURE_PATHS, reason="no story sequencer fixtures")
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda path: path.stem)
def test_story_sequencer_eval(
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
        f"  failures: {result.structural_failures}\n  error: {result.error}"
    )
    if eval_mode != "live":
        assert result.output["order"] == fixture.meta["expected_order"]


# ── the held-out set is fit to judge with (free, every CI run) ─────────────────────────────────


def _reference_ranking(case: dict) -> list[str]:
    """A ranking of the open clips that satisfies every pair (fails when the pairs contradict)."""
    open_ids = set(scoring.case_input(case).open_ids())
    remaining, placed = [c for c in scoring.upload_order(case) if c in open_ids], []
    while remaining:
        free = [
            clip
            for clip in remaining
            if not any(
                later == clip and earlier in remaining for earlier, later in case["precedence"]
            )
        ]
        assert free, f"{case['id']}: the precedence pairs contradict each other"
        placed.append(free[0])
        remaining.remove(free[0])
    return placed


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_heldout_case_is_consistent_and_the_upload_order_fails_it(case: dict) -> None:
    value = scoring.case_input(case)  # the agent accepts it
    open_ids = set(value.open_ids())
    span_of = {str(clip["id"]): clip.get("span") for clip in case["input"]["clips"]}
    assert case["precedence"], "a case with no pairs judges nothing"
    for earlier, later in case["precedence"]:
        # only clips the model may order, and only inside one span: a pair across spans is
        # decided by the creator's own group order, not by the model
        assert {earlier, later} <= open_ids, (earlier, later)
        assert span_of[earlier] == span_of[later], (earlier, later)
    # the pairs can all hold at once: some order of the clips scores 1.0
    story = scoring.played_order(case, _reference_ranking(case))
    assert scoring.precedence_score(case, story) == 1.0
    # ... and the order the clips were added in does not satisfy them
    assert scoring.precedence_score(case, scoring.upload_order(case)) < 1.0
    # a clip the creator placed is where it was, whatever is ranked
    for index, clip in enumerate(scoring.upload_order(case)):
        if clip not in open_ids:
            assert story[index] == clip


def test_heldout_upload_order_baseline_leaves_room_to_be_beaten() -> None:
    baseline = scoring.mean(
        [scoring.precedence_score(case, scoring.upload_order(case)) for case in CASES]
    )
    assert len(CASES) >= 12
    assert baseline + scoring.REQUIRED_MARGIN <= 0.8, baseline


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_heldout_negative_control_the_wrong_order_of_the_same_clips_scores_zero(
    case: dict,
) -> None:
    """The score tells a story order from a wrong one of the SAME clips: the story order played
    backwards satisfies no pair, so "some permutation" never passes for a story."""
    backwards = scoring.played_order(case, list(reversed(_reference_ranking(case))))
    assert scoring.precedence_score(case, backwards) == 0.0


# ── the comparison: recorded live answers, re-scored for free ──────────────────────────────────


def _score_run(run: dict) -> tuple[float, float, list[str]]:
    """(model mean, upload-order mean, problems) for a recorded run, through the real parse."""
    agent = StorySequencerAgent(None)  # type: ignore[arg-type]
    answers = run["answers"]
    model_scores, baseline_scores, problems = [], [], []
    for case in CASES:
        baseline_scores.append(scoring.precedence_score(case, scoring.upload_order(case)))
        raw = answers.get(case["id"], {}).get("raw_text")
        if raw is None:
            problems.append(f"{case['id']}: no recorded answer")
            model_scores.append(0.0)
            continue
        try:
            output = agent.parse(raw, scoring.case_input(case))
        except SchemaError as exc:
            # An answer the agent refuses is upload order in production: scored as such.
            problems.append(f"{case['id']}: refused ({exc})")
            model_scores.append(baseline_scores[-1])
            continue
        played = scoring.played_order(case, output.order if output.decided else None)
        placed = [str(c["id"]) for c in case["input"]["clips"] if not c.get("span")]
        before = scoring.upload_order(case)
        if any(played.index(clip) != before.index(clip) for clip in placed):
            problems.append(f"{case['id']}: a creator-placed clip moved")
        model_scores.append(scoring.precedence_score(case, played))
    return scoring.mean(model_scores), scoring.mean(baseline_scores), problems


def test_recorded_run_beats_upload_order() -> None:
    if not scoring.RECORDED_RUN.exists():
        pytest.skip(
            "no recorded held-out run: it needs an approved paid live run "
            "(tests/evals/README.md, story sequencer)"
        )
    run = json.loads(scoring.RECORDED_RUN.read_text(encoding="utf-8"))
    assert run["prompt_version"] == StorySequencerAgent.spec.prompt_version, (
        "the recorded run is for another prompt: record it again before trusting the comparison"
    )
    model, baseline, problems = _score_run(run)
    assert not [p for p in problems if "moved" in p or "no recorded" in p], problems
    assert model >= baseline + scoring.REQUIRED_MARGIN, (model, baseline, problems)


def _left_alone(case: dict, raw: str | None) -> bool:
    """Whether the played order for ``raw`` is the order the clips were added in."""
    if raw is None:
        return True
    try:
        output = StorySequencerAgent(None).parse(raw, scoring.case_input(case))  # type: ignore[arg-type]
    except SchemaError:
        return True
    played = scoring.played_order(case, output.order if output.decided else None)
    return played == scoring.upload_order(case)


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001 - provenance only
        return "unknown"


def test_live_heldout_run(eval_mode: str, live_model_client) -> None:
    """Paid. Asks the CURRENT agent every held-out case once and records the raw answers."""
    if eval_mode != "live":
        pytest.skip("the held-out run needs --eval-mode=live (an approved paid run)")
    spec = StorySequencerAgent.spec
    answers: dict[str, dict] = {}
    for case in [*CASES, *HELDOUT["restraint"]]:
        recorder = RecordingModelClient(live_model_client)
        ctx = build_eval_run_context(f"story-sequencer-heldout/{case['id']}", is_live=True)
        error = None
        try:
            StorySequencerAgent(recorder).run(scoring.case_input(case), ctx=ctx)
        except Exception as exc:  # noqa: BLE001 - a failed call is data, not a crash
            error = type(exc).__name__
        raw = recorder.invocations[-1].raw_text if recorder.invocations else None
        answers[case["id"]] = {"raw_text": raw, **({"error": error} if error else {})}
    run = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "agent": spec.name,
        "prompt_version": spec.prompt_version,
        "model": spec.model,
        "answers": answers,
    }
    model, baseline, problems = _score_run(run)
    run["scores"] = {"model": round(model, 3), "upload_order": round(baseline, 3)}
    run["problems"] = problems
    # Clips with no reason to prefer an order: did it leave them as they were?
    run["restraint"] = {
        case["id"]: _left_alone(case, answers[case["id"]].get("raw_text"))
        for case in HELDOUT["restraint"]
    }
    scoring.RECORDED_RUN.parent.mkdir(parents=True, exist_ok=True)
    scoring.RECORDED_RUN.write_text(json.dumps(run, ensure_ascii=False, indent=1) + "\n")
    assert model >= baseline + scoring.REQUIRED_MARGIN, (model, baseline, problems)
