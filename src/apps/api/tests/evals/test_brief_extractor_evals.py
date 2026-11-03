"""Replay/live eval gate for nova.creator.brief_extractor (KRI-1189).

The extractor writes the brief for follow-up turns on a rendered edit (rendered edits never
invoke Main Creator). Fixtures are hand-authored golden answers: replay proves the answer shape
and the `meta.restart_brief` check; a paid live run measures the model.
"""

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval
from .runners.restart_brief import restart_brief_failures

AGENT_DIR = "brief_extractor"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda path: path.stem)
def test_brief_extractor_eval(fixture_path: Path, eval_mode: str, live_model_client) -> None:
    fixture = load_fixture(fixture_path)
    result = run_eval(
        fixture, model_client=live_model_client if eval_mode == "live" else None, judge=None
    )
    assert result.passed, f"{result.summary()}: {result.structural_failures} {result.error}"

    restart = fixture.meta.get("restart_brief")
    if restart:
        assert result.output is not None
        failures = restart_brief_failures(result.output.get("brief_updates") or [], restart)
        assert not failures, failures
        if restart.get("cut_restarts") is False and "start over" in fixture.input["user_message"].casefold():
            # KRI-816: "start over ... from scratch" is the typed start-fresh ask instead.
            assert result.output.get("starts_fresh") is True or (
                result.output.get("request_scope") == "rebuild"
            ), result.output


def test_the_restart_check_rejects_the_prod_green3_row():
    """Wrong-content control: the prod Green 3 r2 (job 9bad2d75), the take's transcript in
    `cut_lines` and no `cut_restarts`, must fail both halves of the check."""
    import json

    fixture = Path(__file__).parents[1] / "fixtures" / "kri1176_green3_restart.json"
    prod = json.loads(fixture.read_text())["brief_r2_turn1"]

    failures = restart_brief_failures([prod], {"cut_restarts": True, "speech_cleanup": True})

    assert any("not in its update's own words" in failure for failure in failures), failures
    assert any("no update records cut_restarts" in failure for failure in failures), failures
