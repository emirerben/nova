"""Tests for routes/admin_jobs.py — admin job debug endpoints.

Covers:
  - auth gate (X-Admin-Token)
  - list endpoint pagination + filter shape
  - detail endpoint 400 on invalid uuid
  - detail endpoint 404 on missing job
  - detail endpoint returns empty agent_runs[] for a legacy job
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app

VALID_TOKEN = "test-admin-token"


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _job_row(**overrides) -> SimpleNamespace:
    """Build a fake Job ORM-ish row that the route can read attributes off."""
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="processing",
        job_type="music",
        mode=None,
        template_id=None,
        music_track_id="track-a",
        failure_reason=None,
        error_detail=None,
        current_phase="analyze_clips",
        phase_log=[],
        raw_storage_path="raw/x.mp4",
        selected_platforms=None,
        probe_metadata=None,
        transcript=None,
        scene_cuts=None,
        all_candidates=None,
        assembly_plan=None,
        pipeline_trace=None,
        started_at=None,
        finished_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── Auth ─────────────────────────────────────────────────────────────────────


class TestAdminJobsAuth:
    def test_missing_token_unauthorized(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            res = client.get("/admin/jobs")
        assert res.status_code in (401, 422)

    def test_wrong_token_401(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            res = client.get(
                "/admin/jobs",
                headers={"X-Admin-Token": "wrong"},
            )
        assert res.status_code == 401


# ── List endpoint ────────────────────────────────────────────────────────────


class TestListJobs:
    def test_empty_list(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN

            async def _gen():
                db = AsyncMock()
                # Two execute calls: count, then rows.
                count_res = MagicMock()
                count_res.scalar.return_value = 0
                rows_res = MagicMock()
                rows_res.scalars.return_value.all.return_value = []
                db.execute = AsyncMock(side_effect=[count_res, rows_res])
                yield db

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.get("/admin/jobs", headers={"X-Admin-Token": VALID_TOKEN})
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 200
        body = res.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["limit"] == 50
        assert body["offset"] == 0

    def test_list_includes_agent_run_counts(self, client):
        j = _job_row()
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN

            async def _gen():
                db = AsyncMock()
                count_res = MagicMock()
                count_res.scalar.return_value = 1
                rows_res = MagicMock()
                rows_res.scalars.return_value.all.return_value = [j]
                # third execute = agent_run counts subquery
                counts_res = MagicMock()
                counts_res.fetchall.return_value = [
                    SimpleNamespace(job_id=j.id, total=7, failures=2),
                ]
                db.execute = AsyncMock(side_effect=[count_res, rows_res, counts_res])
                yield db

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.get("/admin/jobs", headers={"X-Admin-Token": VALID_TOKEN})
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 200
        body = res.json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["job_id"] == str(j.id)
        assert item["agent_run_count"] == 7
        assert item["failure_count"] == 2


# ── Detail endpoint ──────────────────────────────────────────────────────────


class TestJobDebug:
    def test_invalid_uuid_returns_400(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN

            async def _gen():
                yield AsyncMock()

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.get(
                    "/admin/jobs/not-a-uuid/debug",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 400

    def test_missing_job_returns_404(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN

            async def _gen():
                db = AsyncMock()
                res_obj = MagicMock()
                res_obj.scalar_one_or_none.return_value = None
                db.execute = AsyncMock(return_value=res_obj)
                yield db

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.get(
                    f"/admin/jobs/{uuid.uuid4()}/debug",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 404

    def test_legacy_job_returns_empty_agent_runs(self, client):
        """Job that ran before this feature: no agent_run rows, no
        pipeline_trace. UI must still render — endpoint must succeed."""
        j = _job_row(pipeline_trace=None)
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN

            async def _gen():
                db = AsyncMock()
                # 1) get(Job) → returns j
                job_res = MagicMock()
                job_res.scalar_one_or_none.return_value = j
                # 2) get(JobClip) → empty
                clips_res = MagicMock()
                clips_res.scalars.return_value.all.return_value = []
                # 3) get(AgentRun) → empty
                runs_res = MagicMock()
                runs_res.scalars.return_value.all.return_value = []
                # job.template_id is None so template lookup is skipped.
                # job.music_track_id="track-a" triggers a music_track lookup.
                mt_res = MagicMock()
                mt_res.scalar_one_or_none.return_value = None
                db.execute = AsyncMock(side_effect=[job_res, clips_res, mt_res, runs_res])
                yield db

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.get(
                    f"/admin/jobs/{j.id}/debug",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 200
        body = res.json()
        assert body["job"]["id"] == str(j.id)
        assert body["agent_runs"] == []
        assert body["job_clips"] == []
        assert body["job"]["pipeline_trace"] is None
