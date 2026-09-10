"""Structural live evals; optional paid semantic judging runs separately in replay."""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "edit_proposal"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(not FIXTURE_PATHS, reason="no edit-proposal fixtures")
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda path: path.stem)
def test_edit_proposal_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
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
    assert result.output is not None
    # README: live calls are ledger capped; unmetered judging runs separately
    # in replay. Keep the source-use invariant as an independent output check.
    if fixture.input.get("video_reuse_policy") == "once":
        video_ids = {row["media_id"] for row in fixture.input["media"] if row["kind"] == "video"}
        used = [
            cut["media_id"]
            for cut in result.output.get("fast_cuts") or []
            if cut["media_id"] in video_ids
        ]
        assert len(used) == len(set(used)), "default plans must not revisit a video"
    if eval_mode == "replay":
        # Golden cassettes pin the intended chapter vocabulary. Live outputs are
        # allowed natural synonyms; optional replay judging scores semantic coverage.
        topics = {beat["topic"].lower() for beat in result.output["story_beats"]}
        expected_topics = set(fixture.meta.get("expected_topics") or [])
        assert all(any(expected in topic for topic in topics) for expected in expected_topics)
