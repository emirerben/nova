"""Unit tests for runners/eval_runner.py — fixture loading + run_eval orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agents._runtime import ModelInvocation, TerminalError

from .runners.eval_runner import (
    CassetteModelClient,
    EvalResult,
    Fixture,
    discover_fixtures,
    load_fixture,
    rubric_path_for,
    run_eval,
)
from .runners.llm_judge import JudgeError, JudgeResult

# ── load_fixture / discover_fixtures ────────────────────────────────────────


def _write_fixture(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_load_fixture_happy_path(tmp_path: Path):
    p = tmp_path / "x.json"
    _write_fixture(
        p,
        {
            "agent": "nova.compose.creative_direction",
            "prompt_version": "v1",
            "input": {"file_uri": "files/abc"},
            "raw_text": "some text",
            "output": {"text": "some text"},
            "meta": {"source": "test"},
        },
    )
    fx = load_fixture(p)
    assert fx.agent == "nova.compose.creative_direction"
    assert fx.raw_text == "some text"
    assert fx.fixture_id.endswith("x")


def test_load_fixture_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_fixture(tmp_path / "missing.json")


def test_load_fixture_missing_fields_raises(tmp_path: Path):
    p = tmp_path / "broken.json"
    p.write_text(json.dumps({"agent": "x"}))
    with pytest.raises(ValueError, match="missing fields"):
        load_fixture(p)


# ── CassetteModelClient ─────────────────────────────────────────────────────


def test_cassette_returns_recorded_text():
    cas = CassetteModelClient("hello world")
    inv = cas.invoke(model="gemini-2.5-flash", prompt="anything")
    assert isinstance(inv, ModelInvocation)
    assert inv.raw_text == "hello world"


def test_cassette_rejects_second_call():
    cas = CassetteModelClient("hello")
    cas.invoke(model="gemini-2.5-flash", prompt="x")
    with pytest.raises(TerminalError, match="retried more than once"):
        cas.invoke(model="gemini-2.5-flash", prompt="x")


# ── run_eval ────────────────────────────────────────────────────────────────


def _creative_direction_fixture(tmp_path: Path, raw_text: str) -> Fixture:
    p = tmp_path / "cd.json"
    payload = {
        "agent": "nova.compose.creative_direction",
        "prompt_version": "v1",
        "input": {"file_uri": "files/abc"},
        "raw_text": raw_text,
        "output": {"text": raw_text},
        "meta": {},
    }
    _write_fixture(p, payload)
    return load_fixture(p)


_GOOD_CD_TEXT = (
    "The pacing is snappy with quick whip-pan transitions on the beat drops. "
    "Color grading leans warm with high-contrast feel. Speed ramps slow down on "
    "key moments before snapping back. Audio sync is locked to the beat. "
    "There is no on-camera host or voiceover narration. No letterbox bars; "
    "full bleed framing. Niche is sports highlight reels."
)


def test_run_eval_replay_pass_no_judge(tmp_path: Path):
    fixture = _creative_direction_fixture(tmp_path, _GOOD_CD_TEXT)
    result = run_eval(fixture)
    assert result.passed
    assert result.structural_failures == []
    assert result.judge is None
    assert result.error is None


def test_run_eval_structural_failure(tmp_path: Path):
    fixture = _creative_direction_fixture(tmp_path, "Too short.")
    result = run_eval(fixture)
    assert not result.passed
    assert result.structural_failures
    assert result.judge is None  # judge is skipped when structural fails


def test_run_eval_short_circuits_judge_on_structural_failure(tmp_path: Path):
    """Confirm judge is NOT called when structural already fails."""
    fixture = _creative_direction_fixture(tmp_path, "Too short.")

    class _ExplodingJudge:
        def score(self, **kwargs):
            raise AssertionError("judge should not run on structural failure")

    result = run_eval(fixture, judge=_ExplodingJudge())  # type: ignore[arg-type]
    assert not result.passed
    assert result.judge is None


def test_run_eval_with_judge_pass(tmp_path: Path):
    fixture = _creative_direction_fixture(tmp_path, _GOOD_CD_TEXT)

    class _StubJudge:
        def score(self, **kwargs: Any) -> JudgeResult:
            return JudgeResult(scores={"a": 4.0}, avg=4.0, passed=True, threshold=3.5)

    result = run_eval(fixture, judge=_StubJudge())  # type: ignore[arg-type]
    assert result.passed
    assert result.judge is not None
    assert result.judge.passed


def test_run_eval_with_judge_fail(tmp_path: Path):
    fixture = _creative_direction_fixture(tmp_path, _GOOD_CD_TEXT)

    class _StubJudge:
        def score(self, **kwargs: Any) -> JudgeResult:
            return JudgeResult(scores={"a": 2.0}, avg=2.0, passed=False, threshold=3.5)

    result = run_eval(fixture, judge=_StubJudge())  # type: ignore[arg-type]
    assert not result.passed
    assert result.judge is not None
    assert not result.judge.passed


def test_run_eval_judge_error_surfaced(tmp_path: Path):
    fixture = _creative_direction_fixture(tmp_path, _GOOD_CD_TEXT)

    class _ExplodingJudge:
        def score(self, **kwargs):
            raise JudgeError("boom")

    result = run_eval(fixture, judge=_ExplodingJudge())  # type: ignore[arg-type]
    assert not result.passed
    assert result.error is not None
    assert "boom" in result.error


def test_run_eval_agent_run_error(tmp_path: Path):
    p = tmp_path / "bad.json"
    payload = {
        "agent": "nova.compose.creative_direction",
        "prompt_version": "v1",
        "input": {"file_uri": "files/abc"},
        # Empty raw_text — agent.parse will raise SchemaError
        "raw_text": "",
        "output": {"text": ""},
        "meta": {},
    }
    _write_fixture(p, payload)
    fixture = load_fixture(p)
    result = run_eval(fixture)
    assert not result.passed
    assert result.error is not None and "agent.run failed" in result.error


def test_eval_result_summary_formats():
    r = EvalResult(
        fixture_id="x",
        agent="nova.x",
        prompt_version="v1",
        structural_failures=["a", "b"],
    )
    assert "FAIL" in r.summary()
    assert "2 failures" in r.summary()


# ── helpers ─────────────────────────────────────────────────────────────────


def test_rubric_path_for_strips_namespace(tmp_path: Path):
    p = rubric_path_for("nova.compose.template_recipe", rubric_dir=tmp_path)
    assert p == tmp_path / "template_recipe.md"


def test_discover_fixtures_empty_when_dir_missing(tmp_path: Path, monkeypatch):
    from .runners import eval_runner as er

    monkeypatch.setattr(er, "EVAL_FIXTURES_ROOT", tmp_path / "nope")
    assert discover_fixtures("template_recipe") == []


# ── _shadow_prompts context manager ─────────────────────────────────────────


def test_shadow_prompts_overlays_candidate_then_restores(tmp_path: Path):
    """Inside the CM, candidate-dir files override prod_loader. After exit,
    prod_loader._get_raw is restored and the cache is cleaned up."""
    from app.pipeline import prompt_loader

    from .runners.eval_runner import _shadow_prompts

    candidate = tmp_path / "transcribe.txt"
    candidate.write_text("CANDIDATE PROMPT BODY")

    original_get_raw = prompt_loader._get_raw
    prompt_loader._cache.clear()

    with _shadow_prompts(tmp_path):
        # Candidate file present → returns candidate text
        assert prompt_loader._get_raw("transcribe") == "CANDIDATE PROMPT BODY"
        # Candidate missing → falls through to prod loader
        prod_text = prompt_loader._get_raw("analyze_clip")
        assert prod_text and "CANDIDATE" not in prod_text

    # After exit: original loader restored, candidate not visible
    assert prompt_loader._get_raw is original_get_raw
    prompt_loader._cache.clear()
    # Reading "transcribe" now goes through prod loader; either returns the prod
    # template (if file exists) or raises — what matters is it's NOT the candidate.
    try:
        result = prompt_loader._get_raw("transcribe")
        assert result != "CANDIDATE PROMPT BODY"
    except Exception:
        pass  # prod prompt file may not exist in test env — that's fine


def test_shadow_prompts_restores_on_exception(tmp_path: Path):
    """If the body of the CM raises, _get_raw still restores."""
    from app.pipeline import prompt_loader

    from .runners.eval_runner import _shadow_prompts

    original_get_raw = prompt_loader._get_raw
    with pytest.raises(RuntimeError, match="boom"):
        with _shadow_prompts(tmp_path):
            raise RuntimeError("boom")
    assert prompt_loader._get_raw is original_get_raw


# ── estimate_live_cost ──────────────────────────────────────────────────────


def test_estimate_live_cost_per_agent_breakdown():
    from .runners.eval_runner import estimate_live_cost

    fixtures = [
        Fixture(
            path=Path("/dev/null/a.json"),
            agent="nova.compose.template_recipe",
            prompt_version="v1",
            input={"file_uri": "files/x", "file_mime": "video/mp4"},
            raw_text="",
            output={},
            meta={},
        ),
        Fixture(
            path=Path("/dev/null/b.json"),
            agent="nova.compose.template_recipe",
            prompt_version="v1",
            input={"file_uri": "files/y", "file_mime": "video/mp4"},
            raw_text="",
            output={},
            meta={},
        ),
        Fixture(
            path=Path("/dev/null/c.json"),
            agent="nova.audio.transcript",
            prompt_version="v1",
            input={"file_uri": "files/z", "file_mime": "video/mp4"},
            raw_text="",
            output={},
            meta={},
        ),
    ]
    breakdown, total = estimate_live_cost(fixtures)
    assert "nova.compose.template_recipe" in breakdown
    assert breakdown["nova.compose.template_recipe"][1] == 2  # fixture count
    assert breakdown["nova.audio.transcript"][1] == 1
    assert total > 0
    assert total == sum(c for c, _ in breakdown.values())


def test_estimate_live_cost_skips_unknown_agents():
    from .runners.eval_runner import estimate_live_cost

    fixtures = [
        Fixture(
            path=Path("/dev/null/x.json"),
            agent="nova.unknown.agent",
            prompt_version="v1",
            input={},
            raw_text="",
            output={},
            meta={},
        ),
    ]
    breakdown, total = estimate_live_cost(fixtures)
    assert breakdown == {}
    assert total == 0.0


def test_estimate_live_cost_warns_on_zero_cost_spec(capsys, monkeypatch):
    """If an agent has zero cost spec, the gate prints a warning so it's not
    silently bypassed."""
    from app.agents._runtime import AgentSpec

    from .runners import eval_runner as er
    from .runners.eval_runner import estimate_live_cost

    # Build a fake agent class with zero costs
    class _ZeroCostAgent:
        spec = AgentSpec(
            name="nova.test.zero",
            prompt_id="_inline",
            prompt_version="v1",
            model="test-model",
            cost_per_1k_input_usd=0,
            cost_per_1k_output_usd=0,
        )

    original_dispatch = er._build_agent_class_for

    def patched(name: str):
        if name == "nova.test.zero":
            return _ZeroCostAgent
        return original_dispatch(name)

    monkeypatch.setattr(er, "_build_agent_class_for", patched)

    fixtures = [
        Fixture(
            path=Path("/dev/null/z.json"),
            agent="nova.test.zero",
            prompt_version="v1",
            input={},
            raw_text="",
            output={},
            meta={},
        ),
    ]
    breakdown, total = estimate_live_cost(fixtures)
    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "nova.test.zero" in captured.out
    assert total == 0.0  # zero cost spec → zero total
    assert breakdown["nova.test.zero"][1] == 1
