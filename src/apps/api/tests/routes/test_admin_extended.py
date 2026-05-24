"""Tests for extended admin endpoints (list, update, metrics, test-job, presigned, recipe-history).

Covers:
- List all templates (empty, with data, pagination)
- Update template metadata + publish + archive
- Metrics endpoint (with and without jobs)
- Test-job creation (valid, template not ready)
- Upload presigned (valid, invalid content type)
- Recipe history (pagination, ordering)
- Shared validation reuse
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import VideoTemplate

VALID_TOKEN = "test-admin-token"


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_template(**kwargs):
    """Create a VideoTemplate-like mock object."""
    defaults = dict(
        id=str(uuid.uuid4()),
        name="Test Template",
        gcs_path="templates/test.mp4",
        analysis_status="ready",
        required_clips_min=3,
        required_clips_max=10,
        published_at=None,
        archived_at=None,
        description=None,
        source_url=None,
        thumbnail_gcs_path=None,
        error_detail=None,
        template_type="standard",
        parent_template_id=None,
        music_track_id=None,
        is_agentic=False,
        use_layer2_default=None,
        created_at=datetime.now(UTC),
        recipe_cached={"slots": [{"position": 1}], "total_duration_s": 30.0},
        recipe_cached_at=datetime.now(UTC),
        audio_gcs_path=None,
        # _template_response reads this; without an explicit None, MagicMock
        # auto-attrs return a MagicMock and Pydantic 500s on serialization.
        lyrics_config=None,
    )
    defaults.update(kwargs)
    mock = MagicMock(spec=VideoTemplate)
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


def _mock_db_with_template(template):
    """Create a mock DB that returns the given template from scalar_one_or_none."""

    async def _gen():
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = template
        mock_db.execute = AsyncMock(return_value=mock_result)
        yield mock_db

    return _gen


def _mock_db_returning(rows, scalar=None):
    """Create a mock DB that returns specified rows from .all() or scalar."""

    async def _gen():
        mock_db = AsyncMock()

        mock_result = MagicMock()
        mock_result.all.return_value = rows
        mock_result.scalars.return_value = mock_result
        if scalar is not None:
            mock_result.scalar.return_value = scalar
            mock_result.scalar_one_or_none.return_value = scalar
            # For count queries
            mock_result.one.return_value = scalar

        mock_db.execute = AsyncMock(return_value=mock_result)
        yield mock_db

    return _gen


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _admin_headers():
    return {"X-Admin-Token": VALID_TOKEN}


# ── List Templates ────────────────────────────────────────────────────────────


class TestListTemplates:
    def test_list_empty(self, client):
        """List returns empty when no templates exist."""

        async def _gen():
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_result.scalar.return_value = 0
            mock_db.execute = AsyncMock(return_value=mock_result)
            yield mock_db

        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _gen
            try:
                res = client.get("/admin/templates", headers=_admin_headers())
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 0
        assert data["templates"] == []


# ── Update Template ───────────────────────────────────────────────────────────


class TestUpdateTemplate:
    def test_publish_ready_template(self, client):
        """Publishing a ready template sets published_at."""
        template = _make_template(analysis_status="ready")

        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.patch(
                    f"/admin/templates/{template.id}",
                    json={"publish": True},
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200

    def test_publish_non_ready_template_returns_409(self, client):
        """Cannot publish a template that is still analyzing."""
        template = _make_template(analysis_status="analyzing")

        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.patch(
                    f"/admin/templates/{template.id}",
                    json={"publish": True},
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 409

    def test_archive_template(self, client):
        """Archiving sets archived_at."""
        template = _make_template(published_at=datetime.now(UTC))

        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.patch(
                    f"/admin/templates/{template.id}",
                    json={"archive": True},
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200

    def test_update_metadata(self, client):
        """Updating name and description succeeds."""
        template = _make_template()

        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.patch(
                    f"/admin/templates/{template.id}",
                    json={"name": "Updated Name", "description": "New desc"},
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200

    def test_update_not_found(self, client):
        """Update returns 404 for nonexistent template."""
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _mock_db_with_template(None)
            try:
                res = client.patch(
                    "/admin/templates/nonexistent",
                    json={"name": "x"},
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404


# ── Metrics ───────────────────────────────────────────────────────────────────


class TestMetrics:
    def test_metrics_with_no_jobs(self, client):
        """Metrics returns zeros when template has no jobs."""
        template = _make_template()

        async def _gen():
            mock_db = AsyncMock()
            call_count = 0

            def make_result(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                mock_r = MagicMock()
                if call_count == 1:
                    # First call: get_template_or_404
                    mock_r.scalar_one_or_none.return_value = template
                else:
                    # Second call: aggregate metrics
                    row = MagicMock()
                    row.total = 0
                    row.successful = 0
                    row.failed = 0
                    row.last_job_at = None
                    mock_r.one.return_value = row
                return mock_r

            mock_db.execute = AsyncMock(side_effect=make_result)
            yield mock_db

        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _gen
            try:
                res = client.get(
                    f"/admin/templates/{template.id}/metrics",
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        data = res.json()
        assert data["total_jobs"] == 0
        assert data["successful_jobs"] == 0
        assert data["failed_jobs"] == 0


# ── Test Job ──────────────────────────────────────────────────────────────────


class TestCreateTestJob:
    def test_create_test_job_template_not_ready(self, client):
        """Test job creation fails if template is still analyzing."""
        template = _make_template(analysis_status="analyzing")

        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.post(
                    f"/admin/templates/{template.id}/test-job",
                    json={"clip_gcs_paths": ["clip1.mp4"]},
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 409

    def test_create_test_job_not_found(self, client):
        """Test job creation fails if template doesn't exist."""
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _mock_db_with_template(None)
            try:
                res = client.post(
                    "/admin/templates/nonexistent/test-job",
                    json={"clip_gcs_paths": ["clip1.mp4"]},
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404

    def test_create_test_job_too_few_clips(self, client):
        """Test job creation fails with too few clips."""
        template = _make_template(required_clips_min=3)

        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.post(
                    f"/admin/templates/{template.id}/test-job",
                    json={"clip_gcs_paths": ["clip1.mp4"]},  # Only 1, need 3
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 422


# ── Upload Presigned ──────────────────────────────────────────────────────────


class TestUploadPresigned:
    def test_invalid_content_type_returns_422(self, client):
        """Presigned upload rejects invalid content types."""
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            res = client.post(
                "/admin/upload-presigned",
                json={"filename": "test.txt", "content_type": "text/plain"},
                headers=_admin_headers(),
            )

        assert res.status_code == 422

    def test_valid_presigned_upload(self, client):
        """Presigned upload succeeds with valid params."""
        mock_blob = MagicMock()
        mock_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed-url"
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        with (
            patch("app.routes.admin.settings") as s,
            patch("app.storage._get_client", return_value=mock_client),
        ):
            s.admin_api_key = VALID_TOKEN
            s.storage_bucket = "nova-test"
            res = client.post(
                "/admin/upload-presigned",
                json={"filename": "template.mp4", "content_type": "video/mp4"},
                headers=_admin_headers(),
            )

        assert res.status_code == 200
        data = res.json()
        assert data["upload_url"].startswith("https://")
        assert data["gcs_path"].startswith("templates/")


# ── Recipe History ────────────────────────────────────────────────────────────


class TestRecipeHistory:
    def test_recipe_history_not_found(self, client):
        """Recipe history returns 404 for nonexistent template."""
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _mock_db_with_template(None)
            try:
                res = client.get(
                    "/admin/templates/nonexistent/recipe-history",
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404


# ── Shared Validation ─────────────────────────────────────────────────────────


class TestSharedValidation:
    @pytest.mark.anyio
    async def test_require_ready_raises_on_analyzing(self):
        """require_ready raises 409 for non-ready templates."""
        from app.services.template_validation import require_ready

        template = _make_template(analysis_status="analyzing")
        with pytest.raises(Exception) as exc_info:
            require_ready(template)
        assert "409" in str(exc_info.value.status_code)

    @pytest.mark.anyio
    async def test_validate_clip_count_too_few(self):
        """validate_clip_count raises 422 when below minimum."""
        from app.services.template_validation import validate_clip_count

        template = _make_template(required_clips_min=3, required_clips_max=10)
        with pytest.raises(Exception) as exc_info:
            validate_clip_count(template, 1)
        assert "422" in str(exc_info.value.status_code)

    @pytest.mark.anyio
    async def test_validate_clip_count_too_many(self):
        """validate_clip_count raises 422 when above maximum."""
        from app.services.template_validation import validate_clip_count

        template = _make_template(required_clips_min=3, required_clips_max=10)
        with pytest.raises(Exception) as exc_info:
            validate_clip_count(template, 15)
        assert "422" in str(exc_info.value.status_code)

    @pytest.mark.anyio
    async def test_validate_clip_count_valid(self):
        """validate_clip_count passes for valid clip count."""
        from app.services.template_validation import validate_clip_count

        template = _make_template(required_clips_min=3, required_clips_max=10)
        validate_clip_count(template, 5)  # Should not raise

    @pytest.mark.anyio
    async def test_validate_clip_count_mixed_media_requires_exact_slot_count(self):
        """Mixed-media templates (any photo slot) need exactly slot_count clips."""
        from app.services.template_validation import validate_clip_count

        recipe = {
            "slots": [
                {"position": 1, "media_type": "video"},
                {"position": 2, "media_type": "photo"},
            ],
            "total_duration_s": 8.0,
        }
        # min/max are deliberately wider — the photo branch must override them
        template = _make_template(required_clips_min=1, required_clips_max=20, recipe_cached=recipe)

        with pytest.raises(Exception) as exc_info:
            validate_clip_count(template, 1)
        assert "422" in str(exc_info.value.status_code)

        with pytest.raises(Exception) as exc_info:
            validate_clip_count(template, 3)
        assert "422" in str(exc_info.value.status_code)

        validate_clip_count(template, 2)  # Exact slot count → ok

    @pytest.mark.anyio
    async def test_validate_clip_total_duration_passes_when_long_enough(self):
        from app.services.template_validation import validate_clip_total_duration

        template = _make_template(recipe_cached={"total_duration_s": 8.5})
        # 4.0 + 5.0 = 9.0s > 8.5s required → ok
        validate_clip_total_duration(template, [4.0, 5.0])

    @pytest.mark.anyio
    async def test_validate_clip_total_duration_rejects_short_clips(self):
        """The Just Fine regression: user has 2 clips totaling < template length."""
        from app.services.template_validation import validate_clip_total_duration

        template = _make_template(recipe_cached={"total_duration_s": 8.5})
        with pytest.raises(Exception) as exc_info:
            validate_clip_total_duration(template, [3.0, 3.0])  # 6.0s < 8.5s
        assert "422" in str(exc_info.value.status_code)
        assert "6.0s" in exc_info.value.detail or "6.0" in exc_info.value.detail
        assert "8.5" in exc_info.value.detail

    @pytest.mark.anyio
    async def test_validate_clip_total_duration_tolerates_subsecond_rounding(self):
        """Browser duration reads round to ~0.1s. 8.4 vs 8.5 should pass."""
        from app.services.template_validation import validate_clip_total_duration

        template = _make_template(recipe_cached={"total_duration_s": 8.5})
        validate_clip_total_duration(template, [8.4])  # within 0.25s tolerance

    @pytest.mark.anyio
    async def test_validate_clip_total_duration_skips_when_no_durations(self):
        """Legacy clients that don't send durations skip the check (no raise)."""
        from app.services.template_validation import validate_clip_total_duration

        template = _make_template(recipe_cached={"total_duration_s": 8.5})
        validate_clip_total_duration(template, None)
        validate_clip_total_duration(template, [])

    @pytest.mark.anyio
    async def test_validate_clip_total_duration_skips_without_recipe_metadata(self):
        """Templates without total_duration_s in recipe (e.g. partial analysis) pass."""
        from app.services.template_validation import validate_clip_total_duration

        template = _make_template(recipe_cached={})  # no total_duration_s
        validate_clip_total_duration(template, [1.0])

    @pytest.mark.anyio
    async def test_validate_clip_total_duration_single_video_short_clip(self):
        """Single-video templates: the lone clip must be at least template length."""
        from app.services.template_validation import validate_clip_total_duration

        template = _make_template(recipe_cached={"total_duration_s": 30.0})
        with pytest.raises(Exception) as exc_info:
            validate_clip_total_duration(template, [10.0])
        assert "422" in str(exc_info.value.status_code)


# ── Reanalyze clears error_detail ────────────────────────────────────────────


class TestReanalyzeErrorDetail:
    def test_reanalyze_clears_error_detail(self, client):
        """Reanalyze sets error_detail=None and analysis_status='analyzing'."""
        template = _make_template(
            analysis_status="failed",
            error_detail="Analysis timed out (exceeded 840s soft limit)",
        )

        mock_redis_instance = MagicMock()

        with (
            patch("app.routes.admin.settings") as s,
            patch("redis.from_url", return_value=mock_redis_instance),
        ):
            s.admin_api_key = VALID_TOKEN
            s.redis_url = "redis://localhost:6379"

            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.post(
                    f"/admin/templates/{template.id}/reanalyze",
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        # Template should have error_detail cleared
        assert template.error_detail is None
        assert template.analysis_status == "analyzing"
        # Redis counter should have been cleared
        mock_redis_instance.delete.assert_called_once_with(f"analyze_attempts:{template.id}")

    def test_error_detail_in_template_response(self, client):
        """TemplateResponse includes error_detail field."""
        template = _make_template(
            analysis_status="failed",
            error_detail="API quota exceeded",
        )

        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.get(
                    f"/admin/templates/{template.id}",
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        data = res.json()
        assert data["error_detail"] == "API quota exceeded"


# ── reanalyze-agentic ?use_layer2 query param ─────────────────────────────────


class TestReanalyzeAgenticLayer2Param:
    """Verify that the ?use_layer2=true query param is accepted and forwarded."""

    def test_reanalyze_agentic_use_layer2_true_enqueues_with_kwarg(self, client):
        """`POST /admin/templates/{id}/reanalyze-agentic?use_layer2=true` must
        enqueue agentic_template_build_task with use_layer2=True, even when
        template.use_layer2_default is None and global flag is False.
        Query param wins absolutely.
        """
        template = _make_template(is_agentic=True, analysis_status="ready", use_layer2_default=None)
        mock_redis_instance = MagicMock()
        mock_task = MagicMock()

        with (
            patch("app.routes.admin.settings") as s,
            patch("redis.from_url", return_value=mock_redis_instance),
            patch(
                "app.tasks.agentic_template_build.agentic_template_build_task",
                mock_task,
            ),
        ):
            s.admin_api_key = VALID_TOKEN
            s.redis_url = "redis://localhost:6379"
            s.text_overlay_v2_enabled = False
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.post(
                    f"/admin/templates/{template.id}/reanalyze-agentic?use_layer2=true",
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        # force=True so the reanalyze always reruns the agent stack instead of
        # cache-hitting on the prior recipe (Bug 2 fix, 2026-05-20).
        # overwrite_overlays defaults False → re-run preserves manual overlays.
        mock_task.delay.assert_called_once_with(
            template.id, use_layer2=True, force=True, overwrite_overlays=False
        )

    def test_reanalyze_agentic_use_layer2_false_enqueues_without_override(self, client):
        """`POST /admin/templates/{id}/reanalyze-agentic` (no param) must
        enqueue with use_layer2=False when template_default=None and global flag=False.
        """
        template = _make_template(is_agentic=True, analysis_status="ready", use_layer2_default=None)
        mock_redis_instance = MagicMock()
        mock_task = MagicMock()

        with (
            patch("app.routes.admin.settings") as s,
            patch("redis.from_url", return_value=mock_redis_instance),
            patch(
                "app.tasks.agentic_template_build.agentic_template_build_task",
                mock_task,
            ),
        ):
            s.admin_api_key = VALID_TOKEN
            s.redis_url = "redis://localhost:6379"
            s.text_overlay_v2_enabled = False
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.post(
                    f"/admin/templates/{template.id}/reanalyze-agentic",
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        mock_task.delay.assert_called_once_with(
            template.id, use_layer2=False, force=True, overwrite_overlays=False
        )

    def test_reanalyze_agentic_overwrite_overlays_threads_flag(self, client):
        """`?overwrite_overlays=true` must reach the task so the rebuild
        regenerates overlays instead of carrying the prior ones forward."""
        template = _make_template(is_agentic=True, analysis_status="ready", use_layer2_default=None)
        mock_redis_instance = MagicMock()
        mock_task = MagicMock()

        with (
            patch("app.routes.admin.settings") as s,
            patch("redis.from_url", return_value=mock_redis_instance),
            patch(
                "app.tasks.agentic_template_build.agentic_template_build_task",
                mock_task,
            ),
        ):
            s.admin_api_key = VALID_TOKEN
            s.redis_url = "redis://localhost:6379"
            s.text_overlay_v2_enabled = False
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.post(
                    f"/admin/templates/{template.id}/reanalyze-agentic"
                    "?use_layer2=true&overwrite_overlays=true",
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        mock_task.delay.assert_called_once_with(
            template.id, use_layer2=True, force=True, overwrite_overlays=True
        )


# ── resolve_use_layer2 pure-function tests ────────────────────────────────────


from app.routes.admin import resolve_use_layer2  # noqa: E402


class TestResolveUseLayer2:
    """Unit tests for the resolve_use_layer2 priority helper."""

    def test_query_param_true_wins_over_false_template_default(self):
        """param=True, template_default=False, global=False → True (param wins)."""
        result = resolve_use_layer2(query_param=True, template_default=False, global_flag=False)
        assert result is True

    def test_query_param_false_wins_over_true_template_default(self):
        """param=False, template_default=True, global=True → False (param wins)."""
        result = resolve_use_layer2(query_param=False, template_default=True, global_flag=True)
        assert result is False

    def test_template_default_true_when_no_param(self):
        """param=None, template_default=True, global=False → True (template default wins)."""
        result = resolve_use_layer2(query_param=None, template_default=True, global_flag=False)
        assert result is True

    def test_template_default_false_when_no_param(self):
        """param=None, template_default=False, global=True → False (template default wins)."""
        result = resolve_use_layer2(query_param=None, template_default=False, global_flag=True)
        assert result is False

    def test_global_flag_true_when_both_unset(self):
        """param=None, template_default=None, global=True → True (global flag wins)."""
        result = resolve_use_layer2(query_param=None, template_default=None, global_flag=True)
        assert result is True

    def test_global_flag_false_when_both_unset(self):
        """param=None, template_default=None, global=False → False (global flag wins)."""
        result = resolve_use_layer2(query_param=None, template_default=None, global_flag=False)
        assert result is False


# ── reanalyze-agentic resolution via template.use_layer2_default ──────────────


class TestReanalyzeAgenticTemplateDefault:
    """Verify per-template use_layer2_default is used when no query param is sent."""

    def test_uses_template_default_true_when_no_query_param(self, client):
        """template.use_layer2_default=True + no query param → task gets use_layer2=True."""
        template = _make_template(is_agentic=True, analysis_status="ready", use_layer2_default=True)
        mock_redis_instance = MagicMock()
        mock_task = MagicMock()

        with (
            patch("app.routes.admin.settings") as s,
            patch("redis.from_url", return_value=mock_redis_instance),
            patch(
                "app.tasks.agentic_template_build.agentic_template_build_task",
                mock_task,
            ),
        ):
            s.admin_api_key = VALID_TOKEN
            s.redis_url = "redis://localhost:6379"
            s.text_overlay_v2_enabled = False  # global flag is off
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.post(
                    f"/admin/templates/{template.id}/reanalyze-agentic",
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        # template default overrides global flag
        mock_task.delay.assert_called_once_with(
            template.id, use_layer2=True, force=True, overwrite_overlays=False
        )

    def test_query_param_false_beats_template_default_true(self, client):
        """?use_layer2=false wins even when template.use_layer2_default=True."""
        template = _make_template(is_agentic=True, analysis_status="ready", use_layer2_default=True)
        mock_redis_instance = MagicMock()
        mock_task = MagicMock()

        with (
            patch("app.routes.admin.settings") as s,
            patch("redis.from_url", return_value=mock_redis_instance),
            patch(
                "app.tasks.agentic_template_build.agentic_template_build_task",
                mock_task,
            ),
        ):
            s.admin_api_key = VALID_TOKEN
            s.redis_url = "redis://localhost:6379"
            s.text_overlay_v2_enabled = True
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.post(
                    f"/admin/templates/{template.id}/reanalyze-agentic?use_layer2=false",
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        # query param=false wins over template_default=True and global=True
        mock_task.delay.assert_called_once_with(
            template.id, use_layer2=False, force=True, overwrite_overlays=False
        )


# ── PUT /admin/templates/{id}/use-layer2-default ──────────────────────────────


class TestSetUseLayer2Default:
    """Verify the PUT /admin/templates/{id}/use-layer2-default endpoint."""

    def test_set_use_layer2_default_true(self, client):
        """PUT with {use_layer2_default: true} stores True on the template row."""
        template = _make_template(use_layer2_default=None)

        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.put(
                    f"/admin/templates/{template.id}/use-layer2-default",
                    json={"use_layer2_default": True},
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        data = res.json()
        assert data["use_layer2_default"] is True

    def test_set_use_layer2_default_false(self, client):
        """PUT with {use_layer2_default: false} stores False on the template row."""
        template = _make_template(use_layer2_default=True)

        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.put(
                    f"/admin/templates/{template.id}/use-layer2-default",
                    json={"use_layer2_default": False},
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        data = res.json()
        assert data["use_layer2_default"] is False

    def test_clear_use_layer2_default_to_null(self, client):
        """PUT with {use_layer2_default: null} clears the field (fall through to global)."""
        template = _make_template(use_layer2_default=True)

        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.put(
                    f"/admin/templates/{template.id}/use-layer2-default",
                    json={"use_layer2_default": None},
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        data = res.json()
        assert data["use_layer2_default"] is None

    def test_missing_field_returns_422(self, client):
        """PUT without the required use_layer2_default field returns 422."""
        template = _make_template(use_layer2_default=None)

        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _mock_db_with_template(template)
            try:
                res = client.put(
                    f"/admin/templates/{template.id}/use-layer2-default",
                    json={},  # missing required field
                    headers=_admin_headers(),
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 422
