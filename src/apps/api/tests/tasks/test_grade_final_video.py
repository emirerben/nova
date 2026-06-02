"""Unit tests for the grade_final_video Celery task body (plan M1 / T1).

All offline — the DB session, GCS download, grader, and AgentRun persistence
are monkeypatched, so this exercises the task's orchestration (cost-cap halt,
verdict persistence, failure persistence) without Postgres or Gemini.

Covers (D3): happy-path persistence, cost-cap halt, Gemini-failure persists a
`failed` AgentRun (not a fake verdict), no-render short-circuit.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.tasks.grade_final_video as gfv
from app.services.video_grader import GradeBand, GradeVerdict, VideoGraderError


class _FakeSession:
    def close(self) -> None:  # noqa: D401 — no-op for the task's session lifecycle
        pass


@pytest.fixture
def captured_runs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every persist_agent_run call the task makes."""
    runs: list[dict[str, Any]] = []

    def _fake_persist(**kwargs: Any) -> None:
        runs.append(kwargs)

    # The task imports persist_agent_run lazily from app.agents._persistence.
    monkeypatch.setattr("app.agents._persistence.persist_agent_run", _fake_persist)
    return runs


@pytest.fixture
def stub_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the sync session + download so no real DB/GCS is touched."""
    monkeypatch.setattr("app.database.sync_session", lambda: _FakeSession())
    monkeypatch.setattr("app.storage.download_to_file", lambda *a, **k: None)


def _verdict(band: GradeBand = GradeBand.AUTO_PASS) -> GradeVerdict:
    return GradeVerdict(
        band=band,
        scores={"hook_strength": 5, "overall_quality": 4},
        avg=4.5,
        confidence=0.9,
        threshold=3.5,
        risk_tag="low",
        reasoning="clean render",
        raw_response='{"scores": {}}',
        tokens_in=5000,
        tokens_out=200,
    )


def _patch_grader(monkeypatch: pytest.MonkeyPatch, *, grade_result=None, grade_exc=None) -> None:
    class _FakeGrader:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def grade(self, path: str) -> GradeVerdict:  # noqa: ARG002
            if grade_exc is not None:
                raise grade_exc
            return grade_result

    monkeypatch.setattr("app.services.video_grader.VideoQualityGrader", _FakeGrader)


# ── Happy path ────────────────────────────────────────────────────────────────


def test_persists_verdict_as_agent_run(
    monkeypatch: pytest.MonkeyPatch, stub_db: None, captured_runs: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(gfv, "_grader_spend_today", lambda session: 0.0)
    monkeypatch.setattr(gfv, "_primary_clip_video_path", lambda s, j: "dev-user/job/final.mp4")
    _patch_grader(monkeypatch, grade_result=_verdict(GradeBand.AUTO_PASS))

    gfv._run_grade(job_id="11111111-1111-1111-1111-111111111111")

    assert len(captured_runs) == 1
    run = captured_runs[0]
    assert run["agent_name"] == gfv.GRADER_AGENT_NAME
    assert run["outcome"] == "ok"
    assert run["output_dict"]["band"] == "auto_pass"
    assert run["output_dict"]["confidence"] == 0.9
    assert run["tokens_in"] == 5000
    assert run["tokens_out"] == 200
    # cost is computed from tokens, non-zero, and persisted.
    assert run["cost_usd"] > 0
    assert run["cost_usd"] == pytest.approx(gfv._estimate_cost_usd(5000, 200))


def test_escalate_verdict_persists_with_risk_tag(
    monkeypatch: pytest.MonkeyPatch, stub_db: None, captured_runs: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(gfv, "_grader_spend_today", lambda session: 0.0)
    monkeypatch.setattr(gfv, "_primary_clip_video_path", lambda s, j: "dev-user/job/final.mp4")
    v = _verdict(GradeBand.ESCALATE)
    v.risk_tag = "low_confidence"
    _patch_grader(monkeypatch, grade_result=v)

    gfv._run_grade(job_id="22222222-2222-2222-2222-222222222222")
    assert captured_runs[0]["output_dict"]["risk_tag"] == "low_confidence"
    assert "summary_line" in captured_runs[0]["output_dict"]


# ── Cost-cap halt ─────────────────────────────────────────────────────────────


def test_cost_cap_halts_before_spending(
    monkeypatch: pytest.MonkeyPatch, stub_db: None, captured_runs: list[dict[str, Any]]
) -> None:
    # Today's spend is already at the ceiling → the task must NOT grade or persist.
    monkeypatch.setattr(gfv, "GRADER_DAILY_COST_CAP_USD", 5.0)
    monkeypatch.setattr(gfv, "_grader_spend_today", lambda session: 5.0)

    graded = {"called": False}

    def _explode(s: Any, j: str) -> str:
        graded["called"] = True
        return "x"

    monkeypatch.setattr(gfv, "_primary_clip_video_path", _explode)

    gfv._run_grade(job_id="33333333-3333-3333-3333-333333333333")

    assert graded["called"] is False  # halted before even resolving the clip
    assert captured_runs == []  # nothing persisted


# ── No render available ───────────────────────────────────────────────────────


def test_no_render_short_circuits(
    monkeypatch: pytest.MonkeyPatch, stub_db: None, captured_runs: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(gfv, "_grader_spend_today", lambda session: 0.0)
    monkeypatch.setattr(gfv, "_primary_clip_video_path", lambda s, j: None)

    gfv._run_grade(job_id="44444444-4444-4444-4444-444444444444")
    assert captured_runs == []


# ── Gemini failure persists a `failed` row, not a fake verdict ────────────────


def test_grader_error_persists_failed_agent_run(
    monkeypatch: pytest.MonkeyPatch, stub_db: None, captured_runs: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(gfv, "_grader_spend_today", lambda session: 0.0)
    monkeypatch.setattr(gfv, "_primary_clip_video_path", lambda s, j: "dev-user/job/final.mp4")
    _patch_grader(monkeypatch, grade_exc=VideoGraderError("gemini timed out"))

    gfv._run_grade(job_id="55555555-5555-5555-5555-555555555555")

    assert len(captured_runs) == 1
    run = captured_runs[0]
    assert run["outcome"] == "failed"
    assert run["output_dict"] is None  # NOT a fabricated verdict
    assert "gemini timed out" in run["error"]
    assert run["cost_usd"] == 0.0


def test_download_failure_does_not_persist(
    monkeypatch: pytest.MonkeyPatch, captured_runs: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr("app.database.sync_session", lambda: _FakeSession())
    monkeypatch.setattr(gfv, "_grader_spend_today", lambda session: 0.0)
    monkeypatch.setattr(gfv, "_primary_clip_video_path", lambda s, j: "dev-user/job/final.mp4")

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("gcs 404")

    monkeypatch.setattr("app.storage.download_to_file", _boom)
    _patch_grader(monkeypatch, grade_result=_verdict())

    gfv._run_grade(job_id="66666666-6666-6666-6666-666666666666")
    # Download failure is logged + dropped; no AgentRun written (no Gemini call happened).
    assert captured_runs == []


# ── The Celery wrapper never raises ───────────────────────────────────────────


def test_task_wrapper_swallows_all_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*, job_id: str) -> None:  # noqa: ARG001
        raise RuntimeError("anything")

    monkeypatch.setattr(gfv, "_run_grade", _boom)

    # `.run()` binds `self` for a bind=True task; the wrapper must catch the
    # error and return None (fire-and-forget) rather than propagate.
    result = gfv.grade_final_video.run(job_id="77777777-7777-7777-7777-777777777777")
    assert result is None
