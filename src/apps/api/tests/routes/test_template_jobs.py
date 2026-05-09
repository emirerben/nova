"""Unit tests for routes/template_jobs.py — template job creation and status."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import VideoTemplate


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
    required_inputs: list | None = None,
    recipe_cached: dict | None = None,
) -> MagicMock:
    t = MagicMock(spec=VideoTemplate)
    t.id = "template-123"
    t.analysis_status = status
    t.required_clips_min = min_clips
    t.required_clips_max = max_clips
    t.required_inputs = required_inputs or []
    t.recipe_cached = recipe_cached or {}
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


class TestCreateTemplateJobInputs:
    """Tests for the per-template `inputs` payload + required_inputs validation."""

    def _post(self, client, template, body_inputs: dict | None = None, mocker=None):
        # Stub Celery dispatch so the test never tries to broker tasks.
        if mocker is not None:
            mocker.patch(
                "app.tasks.template_orchestrate.orchestrate_template_job.delay"
            )
            mocker.patch(
                "app.tasks.template_orchestrate.orchestrate_single_video_job.delay"
            )
        body = {
            "template_id": "template-123",
            "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],
            "selected_platforms": ["tiktok"],
        }
        if body_inputs is not None:
            body["inputs"] = body_inputs
        app.dependency_overrides[get_db] = _db_with_template(template)
        try:
            return client.post("/template-jobs", json=body)
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_create_job_empty_inputs_no_required(self, client, mocker):
        """Template with no required_inputs accepts empty inputs and creates a job."""
        res = self._post(client, _make_template(required_inputs=[]), {}, mocker)
        assert res.status_code == 201

    def test_create_job_unknown_input_key(self, client, mocker):
        """Submitting a key not declared in required_inputs is rejected."""
        tpl = _make_template(
            required_inputs=[{"key": "location", "label": "Location", "max_length": 50}],
        )
        res = self._post(client, tpl, {"foo": "bar"}, mocker)
        assert res.status_code == 422
        assert "Unknown" in res.json()["detail"]

    def test_create_job_missing_required_input(self, client, mocker):
        """A required input that's empty is rejected."""
        tpl = _make_template(
            required_inputs=[
                {"key": "location", "label": "Where filmed?", "required": True, "max_length": 50}
            ],
        )
        res = self._post(client, tpl, {}, mocker)
        assert res.status_code == 422
        assert "required" in res.json()["detail"].lower()

    def test_create_job_oversized_input(self, client, mocker):
        """An input value exceeding declared max_length is rejected."""
        tpl = _make_template(
            required_inputs=[{"key": "location", "label": "Location", "max_length": 50}],
        )
        res = self._post(client, tpl, {"location": "x" * 51}, mocker)
        assert res.status_code == 422
        assert "exceeds" in res.json()["detail"]

    def test_create_job_optional_input_omitted(self, client, mocker):
        """Optional input may be omitted; the job is still accepted."""
        tpl = _make_template(
            required_inputs=[
                {"key": "location", "label": "Location", "required": False, "max_length": 50}
            ],
        )
        res = self._post(client, tpl, {}, mocker)
        assert res.status_code == 201

    def test_create_job_input_count_capped(self, client, mocker):
        """Defensive cap: more than 10 input keys is rejected at the schema layer."""
        too_many = {f"k{i}": "v" for i in range(11)}
        tpl = _make_template(required_inputs=[])
        res = self._post(client, tpl, too_many, mocker)
        assert res.status_code == 422

    def test_create_job_clip_count_min_max(self, client, mocker):
        """Regression for #58: clip count validated against template min/max."""
        # Below min
        tpl = _make_template(min_clips=7, max_clips=10)
        app.dependency_overrides[get_db] = _db_with_template(tpl)
        try:
            res = client.post("/template-jobs", json={
                "template_id": "template-123",
                "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],
                "selected_platforms": ["tiktok"],
                "inputs": {},
            })
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 422

        # Above max
        tpl = _make_template(min_clips=2, max_clips=3)
        app.dependency_overrides[get_db] = _db_with_template(tpl)
        try:
            res = client.post("/template-jobs", json={
                "template_id": "template-123",
                "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],
                "selected_platforms": ["tiktok"],
                "inputs": {},
            })
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 422

        # In range — happy path
        mocker.patch("app.tasks.template_orchestrate.orchestrate_template_job.delay")
        mocker.patch("app.tasks.template_orchestrate.orchestrate_single_video_job.delay")
        tpl = _make_template(min_clips=2, max_clips=10)
        app.dependency_overrides[get_db] = _db_with_template(tpl)
        try:
            res = client.post("/template-jobs", json={
                "template_id": "template-123",
                "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],
                "selected_platforms": ["tiktok"],
                "inputs": {},
            })
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 201


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
