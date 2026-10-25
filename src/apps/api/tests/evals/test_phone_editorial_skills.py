"""Synthetic decision oracles and real provider-boundary checks, never quality claims."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.agents._skills import load_skill
from tests.evals.runners.eval_runner import load_fixture, run_eval
from tests.evals.test_runtime_creative_skills import CapturingClient

CORPUS = Path(__file__).parents[1] / "fixtures" / "phone_editorial_skills"
CASES = sorted(CORPUS.glob("*.json"))


def assert_expected(actual, expected):
    """Match only declared decisions; keep ordered lists exact and ignore extra fields."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        for key, value in expected.items():
            assert key in actual, f"Missing decision {key}"
            assert_expected(actual[key], value)
    elif isinstance(expected, list):
        assert isinstance(actual, list)
        assert len(actual) == len(expected), "Wrong number of selected beats or media"
        for value, wanted in zip(actual, expected, strict=True):
            assert_expected(value, wanted)
    else:
        assert actual == expected


@pytest.mark.parametrize("path", CASES, ids=lambda path: path.stem)
def test_editorial_decisions(path, eval_mode, live_model_client):
    fixture = load_fixture(path)
    result = run_eval(fixture, model_client=live_model_client if eval_mode == "live" else None)
    assert result.passed, f"{result.summary()}: {result.structural_failures}"
    assert_expected(result.output, fixture.meta["expected"])


@pytest.mark.parametrize("path", CASES, ids=lambda path: path.stem)
def test_wrong_content_passes_structure_but_fails_decision_oracle(path):
    fixture = load_fixture(path)
    wrong = replace(fixture, raw_text=fixture.meta["wrong_raw_text"])
    result = run_eval(wrong)
    assert result.passed, f"Control must be structurally plausible: {result.summary()}"
    with pytest.raises(AssertionError):
        assert_expected(result.output, fixture.meta["expected"])


@pytest.mark.parametrize("path", CASES, ids=lambda path: path.stem)
def test_editorial_skills_reach_provider_with_source_context(path):
    fixture = load_fixture(path)
    client = CapturingClient(fixture.raw_text)
    result = run_eval(fixture, model_client=client)
    assert result.passed, result.summary()
    for name in fixture.meta["skills"]:
        assert load_skill(name) in client.prompt
    assert client.invocations == 1
    assert "$creator_request" not in client.prompt
    assert "description:" not in load_skill("edit-story")


def test_corpus_has_required_formats_and_language():
    cases = [load_fixture(path) for path in CASES]
    assert len(cases) >= 12
    assert {case.agent for case in cases} == {
        "nova.creator.main",
        "nova.plan.clip_request_resolver",
        "nova.plan.speech_excerpt_planner",
        "nova.compose.narrated_clip_alignment",
        "nova.plan.slide_post_composer",
    }
    assert any("Köprü" in json.dumps(case.input, ensure_ascii=False) for case in cases)
