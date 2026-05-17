"""Unit tests for routes/admin.py — admin template endpoints."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app

VALID_TOKEN = "test-admin-token"


def _override_db():
    """Yield a mock async DB session."""
    mock_db = AsyncMock()
    yield mock_db


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


class TestAdminAuth:
    def test_missing_token_returns_422_or_401(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.post(
                "/admin/templates", json={"name": "test", "gcs_path": "templates/test.mp4"}
            )
        # Missing required header → 422 (header validation) or 401 (auth check)
        assert res.status_code in (401, 422)

    def test_wrong_token_returns_401(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.post(
                "/admin/templates",
                json={"name": "test", "gcs_path": "templates/test.mp4"},
                headers={"X-Admin-Token": "wrong-token"},
            )
        assert res.status_code == 401

    def test_no_admin_key_configured_returns_503(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = ""
            res = client.post(
                "/admin/templates",
                json={"name": "test", "gcs_path": "templates/test.mp4"},
                headers={"X-Admin-Token": "any-token"},
            )
        assert res.status_code == 503


class TestAdminTemplateValidation:
    def test_gcs_path_must_start_with_templates(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.post(
                "/admin/templates",
                json={"name": "test", "gcs_path": "uploads/test.mp4"},  # wrong prefix
                headers={"X-Admin-Token": VALID_TOKEN},
            )
        assert res.status_code == 422

    def test_gcs_object_not_found_returns_422(self, client):
        with (
            patch("app.routes.admin.settings") as mock_settings,
            patch("app.storage.object_exists", return_value=False),
        ):
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.post(
                    "/admin/templates",
                    json={"name": "test", "gcs_path": "templates/missing.mp4"},
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 422


class TestGetTemplate:
    def test_not_found_returns_404(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN

            async def _mock_db_gen():
                mock_db = AsyncMock()
                mock_result = MagicMock()
                mock_result.scalar_one_or_none.return_value = None
                mock_db.execute = AsyncMock(return_value=mock_result)
                yield mock_db

            app.dependency_overrides[get_db] = _mock_db_gen
            try:
                res = client.get(
                    "/admin/templates/nonexistent-id",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404


# ── /admin/templates/{id}/debug ──────────────────────────────────────────────


def _template_row(**overrides) -> SimpleNamespace:
    base = dict(
        id="tpl_test",
        name="Tiki Welcome",
        analysis_status="ready",
        template_type="standard",
        is_agentic=False,
        gcs_path="templates/tiki.mp4",
        audio_gcs_path=None,
        music_track_id=None,
        error_detail=None,
        recipe_cached=None,
        recipe_cached_at=None,
        created_at=datetime.now(UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _run_row(name: str = "nova.compose.template_recipe") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        segment_idx=None,
        agent_name=name,
        prompt_version="1",
        model="gemini-2.5-pro",
        outcome="ok",
        attempts=1,
        tokens_in=120,
        tokens_out=80,
        cost_usd=None,
        latency_ms=900,
        error_message=None,
        input_json={"k": "v"},
        output_json={"answer": "y"},
        raw_text=None,
        created_at=datetime.now(UTC),
    )


class TestTemplateDebug:
    def test_template_debug_requires_admin(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.get(
                "/admin/templates/tpl_test/debug",
                headers={"X-Admin-Token": "wrong"},
            )
        assert res.status_code == 401

    def test_template_debug_404_for_unknown_template(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN

            async def _gen():
                db = AsyncMock()
                # get_template_or_404 → no template
                tpl_res = MagicMock()
                tpl_res.scalar_one_or_none.return_value = None
                db.execute = AsyncMock(return_value=tpl_res)
                yield db

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.get(
                    "/admin/templates/missing/debug",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 404

    def test_template_debug_returns_agent_runs(self, client):
        tpl = _template_row(
            id="tpl_a",
            recipe_cached={"slots": [{"i": 0}]},
            recipe_cached_at=datetime.now(UTC),
        )
        runs = [_run_row("nova.compose.template_recipe"), _run_row("nova.audio.beat_aligner")]

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN

            async def _gen():
                db = AsyncMock()
                # Execute order: VideoTemplate (via get_template_or_404), then AgentRun
                tpl_res = MagicMock()
                tpl_res.scalar_one_or_none.return_value = tpl
                runs_res = MagicMock()
                runs_res.scalars.return_value.all.return_value = runs
                db.execute = AsyncMock(side_effect=[tpl_res, runs_res])
                yield db

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.get(
                    "/admin/templates/tpl_a/debug",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 200
        body = res.json()
        assert body["template"]["id"] == "tpl_a"
        assert body["template"]["name"] == "Tiki Welcome"
        assert body["template"]["analysis_status"] == "ready"
        assert [r["agent_name"] for r in body["template_agent_runs"]] == [
            "nova.compose.template_recipe",
            "nova.audio.beat_aligner",
        ]
        assert body["recipe_cached"] == {"slots": [{"i": 0}]}

    def test_template_debug_empty_runs(self, client):
        tpl = _template_row(id="tpl_b", analysis_status="analyzing")

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN

            async def _gen():
                db = AsyncMock()
                tpl_res = MagicMock()
                tpl_res.scalar_one_or_none.return_value = tpl
                runs_res = MagicMock()
                runs_res.scalars.return_value.all.return_value = []
                db.execute = AsyncMock(side_effect=[tpl_res, runs_res])
                yield db

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.get(
                    "/admin/templates/tpl_b/debug",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 200
        body = res.json()
        assert body["template_agent_runs"] == []
        assert body["recipe_cached"] is None
        assert body["template"]["analysis_status"] == "analyzing"

    def test_template_debug_caps_at_100_runs(self, client):
        """Route caps result set so a heavily re-analyzed template doesn't
        produce a massive payload. The SQL ``LIMIT 100`` does the work;
        this test confirms the route doesn't add a second layer of
        filtering that would silently truncate further.
        """
        tpl = _template_row(id="tpl_c")
        runs = [_run_row() for _ in range(100)]

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN

            async def _gen():
                db = AsyncMock()
                tpl_res = MagicMock()
                tpl_res.scalar_one_or_none.return_value = tpl
                runs_res = MagicMock()
                runs_res.scalars.return_value.all.return_value = runs
                db.execute = AsyncMock(side_effect=[tpl_res, runs_res])
                yield db

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.get(
                    "/admin/templates/tpl_c/debug",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 200
        body = res.json()
        assert len(body["template_agent_runs"]) == 100
