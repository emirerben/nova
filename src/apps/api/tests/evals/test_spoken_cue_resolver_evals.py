"""Replay/live eval gate for the spoken cue resolver (KRI-1356).

The golden is hand-authored from the de1 voiceover words: replay proves parse and the
structural contract on that answer, never what the live model answers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "spoken_cue_resolver"
AGENT_NAME = "nova.compose.spoken_cue_resolver"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.skipif(not FIXTURE_PATHS, reason="no spoken cue resolver fixtures")
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda path: path.stem)
def test_spoken_cue_resolver_eval(
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
    expected = fixture.meta.get("expected")
    if expected is not None:
        spans = {
            row["cue_id"]: (
                [row["start_word_id"], row["end_word_id"]] if row["start_word_id"] else None
            )
            for row in result.output["resolutions"]
        }
        assert spans == expected
