"""Regression tests for capped/slim admin job debug context runs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.routes._admin_schemas import agent_run_to_payload_summary
from app.routes.admin_jobs import CONTEXT_RUNS_CAP, JobRuntimePayload

VALID_TOKEN = "test-admin-token"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _stub_runtime():
    stub = JobRuntimePayload(state="unknown", worker=None, task_id=None, queue_position=None)
    with patch("app.routes.admin_jobs._resolve_runtime", return_value=stub):
        yield


def _job_row(**overrides) -> SimpleNamespace:
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="processing",
        job_type="template",
        mode=None,
        template_id="template-1",
        music_track_id="track-1",
        failure_reason=None,
        error_detail=None,
        current_phase="rendering",
        phase_log=[],
        raw_storage_path=None,
        selected_platforms=None,
        probe_metadata=None,
        transcript=None,
        scene_cuts=None,
        all_candidates=None,
        assembly_plan=None,
        pipeline_trace=None,
        started_at=None,
        finished_at=None,
        created_at=now,
        updated_at=now,
        celery_task_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _agent_run(name: str, created_at: datetime, *, full: bool = False) -> SimpleNamespace:
    row = SimpleNamespace(
        id=uuid.uuid4(),
        segment_idx=None,
        agent_name=name,
        prompt_version="1",
        model="gemini-2.5-pro",
        outcome="ok",
        attempts=1,
        tokens_in=10,
        tokens_out=20,
        cost_usd=0.01,
        latency_ms=123,
        error_message=None,
        created_at=created_at,
    )
    if full:
        row.input_json = {"input": name}
        row.output_json = {"output": name}
        row.raw_text = "raw"
    return row


def _scalar_result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _rows_result(values):
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    return result


def test_debug_caps_context_runs_and_omits_heavy_io(client: TestClient) -> None:
    now = datetime.now(UTC)
    job = _job_row()
    template_runs = [
        _agent_run(f"template-{i:03d}", now - timedelta(seconds=i))
        for i in range(CONTEXT_RUNS_CAP + 5)
    ]
    track_runs = [
        _agent_run(f"track-{i:03d}", now - timedelta(seconds=i))
        for i in range(CONTEXT_RUNS_CAP + 5)
    ]
    job_run = _agent_run("job-time", now, full=True)
    template = SimpleNamespace(
        id=job.template_id,
        name="Template",
        analysis_status="ready",
        recipe_cached=None,
        audio_gcs_path=None,
        error_detail=None,
    )
    music_track = SimpleNamespace(
        id=job.music_track_id,
        title="Track",
        artist="Artist",
        recipe_cached={"slots": []},
    )

    with patch("app.routes.admin.settings") as settings:
        settings.admin_api_key = VALID_TOKEN

        async def _gen():
            db = AsyncMock()
            db.execute = AsyncMock(
                side_effect=[
                    _scalar_result(job),
                    _rows_result([]),
                    _scalar_result(template),
                    _scalar_result(music_track),
                    _rows_result([job_run]),
                    _rows_result(template_runs),
                    _rows_result(track_runs),
                ]
            )
            yield db

        app.dependency_overrides[get_db] = _gen
        try:
            res = client.get(f"/admin/jobs/{job.id}/debug", headers={"X-Admin-Token": VALID_TOKEN})
        finally:
            app.dependency_overrides.pop(get_db, None)

    assert res.status_code == 200
    body = res.json()
    assert len(body["template_agent_runs"]) == CONTEXT_RUNS_CAP
    assert len(body["track_agent_runs"]) == CONTEXT_RUNS_CAP
    assert body["template_agent_runs_has_more"] is True
    assert body["track_agent_runs_has_more"] is True
    assert body["context_runs_cap"] == CONTEXT_RUNS_CAP

    for item in body["template_agent_runs"] + body["track_agent_runs"]:
        assert "input_json" not in item
        assert "output_json" not in item
        assert "raw_text" not in item

    assert body["agent_runs"][0]["input_json"] == {"input": "job-time"}
    assert body["agent_runs"][0]["output_json"] == {"output": "job-time"}
    assert body["agent_runs"][0]["raw_text"] == "raw"

    assert body["template_agent_runs"][0]["agent_name"] == "template-000"
    assert body["template_agent_runs"][-1]["agent_name"] == f"template-{CONTEXT_RUNS_CAP - 1:03d}"
    assert body["track_agent_runs"][0]["agent_name"] == "track-000"
    assert body["track_agent_runs"][-1]["agent_name"] == f"track-{CONTEXT_RUNS_CAP - 1:03d}"


def test_context_run_summary_serializer_never_touches_heavy_io() -> None:
    class PoisonedRun:
        id = uuid.uuid4()
        segment_idx = None
        agent_name = "template"
        prompt_version = "1"
        model = "m"
        outcome = "ok"
        attempts = 1
        tokens_in = 1
        tokens_out = 2
        cost_usd = None
        latency_ms = 3
        error_message = None
        created_at = datetime.now(UTC)

        @property
        def input_json(self):  # pragma: no cover - failing path proves the regression
            raise AssertionError("summary serializer touched input_json")

        @property
        def output_json(self):  # pragma: no cover
            raise AssertionError("summary serializer touched output_json")

        @property
        def raw_text(self):  # pragma: no cover
            raise AssertionError("summary serializer touched raw_text")

    payload = agent_run_to_payload_summary(PoisonedRun())

    assert payload.agent_name == "template"
    assert not hasattr(payload, "input_json")
    assert not hasattr(payload, "output_json")
    assert not hasattr(payload, "raw_text")


def test_null_template_and_track_ids_skip_context_queries(client: TestClient) -> None:
    job = _job_row(template_id=None, music_track_id=None)
    executed = []

    with patch("app.routes.admin.settings") as settings:
        settings.admin_api_key = VALID_TOKEN

        async def _gen():
            db = AsyncMock()

            async def _execute(stmt, *args, **kwargs):
                executed.append(str(stmt))
                if len(executed) == 1:
                    return _scalar_result(job)
                return _rows_result([])

            db.execute = _execute
            yield db

        app.dependency_overrides[get_db] = _gen
        try:
            res = client.get(f"/admin/jobs/{job.id}/debug", headers={"X-Admin-Token": VALID_TOKEN})
        finally:
            app.dependency_overrides.pop(get_db, None)

    assert res.status_code == 200
    body = res.json()
    assert body["template_agent_runs"] == []
    assert body["template_agent_runs_has_more"] is False
    assert body["track_agent_runs"] == []
    assert body["track_agent_runs_has_more"] is False
    assert len(executed) == 3
    assert all("template_id IS NULL" not in stmt for stmt in executed)
    assert all("music_track_id IS NULL" not in stmt for stmt in executed)
