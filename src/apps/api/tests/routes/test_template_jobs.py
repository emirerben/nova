"""Unit tests for routes/template_jobs.py — template job creation and status."""

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import Job, VideoTemplate


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _db_with_template(template: object | None):
    """Return a DB dependency override that returns the given template."""
    async def _gen():
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = template
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()
        yield mock_db
    return _gen


def _make_template(
    status: str = "ready",
    min_clips: int = 5,
    max_clips: int = 10,
) -> MagicMock:
    t = MagicMock(spec=VideoTemplate)
    t.id = "template-123"
    t.analysis_status = status
    t.required_clips_min = min_clips
    t.required_clips_max = max_clips
    return t


class TestCreateTemplateJobValidation:
    def test_too_few_clips_in_request_returns_422(self, client):
        """The pydantic validator requires ≥1 clip at the schema level."""
        app.dependency_overrides[get_db] = _db_with_template(_make_template())
        try:
            res = client.post("/template-jobs", json={
                "template_id": "template-123",
                "clip_gcs_paths": [],  # empty list
                "selected_platforms": ["tiktok"],
            })
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 422

    def test_too_many_clips_in_request_returns_422(self, client):
        """The pydantic validator caps at 20 clips."""
        app.dependency_overrides[get_db] = _db_with_template(_make_template())
        try:
            res = client.post("/template-jobs", json={
                "template_id": "template-123",
                "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(25)],  # > 20
                "selected_platforms": ["tiktok"],
            })
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 422

    def test_invalid_platform_returns_422(self, client):
        res = client.post("/template-jobs", json={
            "template_id": "template-123",
            "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],
            "selected_platforms": ["snapchat"],  # not valid
        })
        assert res.status_code == 422

    def test_template_not_found_returns_404(self, client):
        app.dependency_overrides[get_db] = _db_with_template(None)
        try:
            res = client.post("/template-jobs", json={
                "template_id": "nonexistent",
                "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],
                "selected_platforms": ["tiktok"],
            })
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 404

    def test_template_not_ready_returns_409(self, client):
        app.dependency_overrides[get_db] = _db_with_template(_make_template(status="analyzing"))
        try:
            res = client.post("/template-jobs", json={
                "template_id": "template-123",
                "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],
                "selected_platforms": ["tiktok"],
            })
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 409
        assert "analyzing" in res.json()["detail"]

    def test_below_template_min_clips_returns_422(self, client):
        app.dependency_overrides[get_db] = _db_with_template(_make_template(min_clips=7))
        try:
            res = client.post("/template-jobs", json={
                "template_id": "template-123",
                "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],  # < min 7
                "selected_platforms": ["tiktok"],
            })
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 422
        assert "7" in res.json()["detail"]

    def test_above_template_max_clips_returns_422(self, client):
        app.dependency_overrides[get_db] = _db_with_template(_make_template(max_clips=3))
        try:
            res = client.post("/template-jobs", json={
                "template_id": "template-123",
                "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],  # > max 3
                "selected_platforms": ["tiktok"],
            })
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 422
        assert "3" in res.json()["detail"]


class TestGetTemplateJobStatus:
    def test_status_not_found_returns_404(self, client):
        async def _gen():
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = None
            mock_db.execute = AsyncMock(return_value=mock_result)
            yield mock_db

        app.dependency_overrides[get_db] = _gen
        try:
            res = client.get("/template-jobs/00000000-0000-0000-0000-000000000001/status")
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 404

    def test_invalid_uuid_returns_404(self, client):
        res = client.get("/template-jobs/not-a-uuid/status")
        assert res.status_code == 404
