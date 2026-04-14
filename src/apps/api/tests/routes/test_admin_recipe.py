"""Tests for GET/PUT /admin/templates/{id}/recipe endpoints."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app

VALID_TOKEN = "test-admin-token"


def _make_recipe(**overrides):
    """Build a minimal valid recipe dict."""
    base = {
        "shot_count": 3,
        "total_duration_s": 10.0,
        "hook_duration_s": 2.0,
        "slots": [
            {
                "position": 1,
                "target_duration_s": 3.0,
                "priority": 5,
                "slot_type": "hook",
                "transition_in": "hard-cut",
                "color_hint": "warm",
                "speed_factor": 1.0,
                "energy": 7.0,
                "text_overlays": [
                    {
                        "role": "hook",
                        "text": "Welcome",
                        "position": "center",
                        "effect": "pop-in",
                        "font_style": "sans",
                        "text_size": "large",
                        "text_color": "#FFFFFF",
                        "start_s": 0.0,
                        "end_s": 2.0,
                        "start_s_override": None,
                        "end_s_override": None,
                        "has_darkening": False,
                        "has_narrowing": False,
                        "sample_text": "",
                        "font_cycle_accel_at_s": None,
                    }
                ],
            },
            {
                "position": 2,
                "target_duration_s": 4.0,
                "priority": 3,
                "slot_type": "broll",
                "transition_in": "dissolve",
                "color_hint": "none",
                "speed_factor": 1.2,
                "energy": 5.0,
                "text_overlays": [],
            },
            {
                "position": 3,
                "target_duration_s": 3.0,
                "priority": 2,
                "slot_type": "outro",
                "transition_in": "whip-pan",
                "color_hint": "cool",
                "speed_factor": 1.0,
                "energy": 3.0,
                "text_overlays": [],
            },
        ],
        "copy_tone": "casual",
        "caption_style": "lowercase",
        "beat_timestamps_s": [0.0, 1.5, 3.0],
        "creative_direction": "energetic travel vibe",
        "transition_style": "fast cuts",
        "color_grade": "warm",
        "pacing_style": "fast",
        "sync_style": "cut-on-beat",
        "interstitials": [
            {
                "type": "fade-black-hold",
                "after_slot": 2,
                "hold_s": 0.5,
                "hold_color": "#000000",
                "animate_s": 0.3,
            }
        ],
    }
    base.update(overrides)
    return base


def _mock_template(recipe_cached=None, analysis_status="ready"):
    """Create a mock VideoTemplate."""
    t = MagicMock()
    t.id = "tmpl-123"
    t.name = "Test Template"
    t.analysis_status = analysis_status
    t.recipe_cached = recipe_cached
    t.recipe_cached_at = datetime.now(UTC) if recipe_cached else None
    return t


def _mock_version(version_id=None, recipe=None):
    """Create a mock TemplateRecipeVersion."""
    v = MagicMock()
    v.id = uuid.UUID(version_id) if version_id else uuid.uuid4()
    v.recipe = recipe or _make_recipe()
    v.trigger = "initial_analysis"
    v.created_at = datetime.now(UTC)
    return v


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


class TestGetRecipe:
    def test_returns_recipe_for_ready_template(self, client):
        recipe = _make_recipe()
        template = _mock_template(recipe_cached=recipe)
        version = _mock_version()

        mock_db = AsyncMock()

        # get_template_or_404 query
        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template

        # latest version query
        version_result = MagicMock()
        version_result.scalar_one_or_none.return_value = version

        # count query
        count_result = MagicMock()
        count_result.scalar.return_value = 2

        mock_db.execute.side_effect = [template_result, version_result, count_result]

        def _override_db():
            yield mock_db

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.get(
                    "/admin/templates/tmpl-123/recipe",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        data = res.json()
        assert data["version_number"] == 2
        assert data["recipe"]["shot_count"] == 3
        assert len(data["recipe"]["slots"]) == 3

    def test_404_for_missing_template(self, client):
        mock_db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = result

        def _override_db():
            yield mock_db

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.get(
                    "/admin/templates/nonexistent/recipe",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404

    def test_409_for_analyzing_template(self, client):
        template = _mock_template(analysis_status="analyzing")

        mock_db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = template
        mock_db.execute.return_value = result

        def _override_db():
            yield mock_db

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.get(
                    "/admin/templates/tmpl-123/recipe",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 409

    def test_auth_required(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.get("/admin/templates/tmpl-123/recipe")
        # Missing header → 422 (validation) or 401
        assert res.status_code in (401, 422)


class TestSaveRecipe:
    def test_saves_valid_recipe(self, client):
        recipe = _make_recipe()
        template = _mock_template(recipe_cached=recipe)
        version_id = str(uuid.uuid4())
        existing_version = _mock_version(version_id=version_id)

        mock_db = AsyncMock()

        # get_template_or_404
        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template

        # optimistic lock check
        lock_result = MagicMock()
        lock_result.scalar_one_or_none.return_value = existing_version

        # count query
        count_result = MagicMock()
        count_result.scalar.return_value = 3

        mock_db.execute.side_effect = [template_result, lock_result, count_result]

        # Mock version refresh
        _mock_version()
        mock_db.refresh = AsyncMock()

        def _override_db():
            yield mock_db

        new_recipe = _make_recipe(copy_tone="exciting")

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.put(
                    "/admin/templates/tmpl-123/recipe",
                    headers={"X-Admin-Token": VALID_TOKEN},
                    json={
                        "recipe": new_recipe,
                        "base_version_id": version_id,
                    },
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        data = res.json()
        assert data["recipe"]["copy_tone"] == "exciting"
        assert data["version_number"] == 3

    def test_rejects_invalid_transition(self, client):
        """Invalid enum values should be rejected by Pydantic."""
        recipe = _make_recipe()
        recipe["slots"][0]["transition_in"] = "invalid-transition"

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.put(
                "/admin/templates/tmpl-123/recipe",
                headers={"X-Admin-Token": VALID_TOKEN},
                json={"recipe": recipe, "base_version_id": None},
            )

        assert res.status_code == 422

    def test_rejects_empty_slots(self, client):
        recipe = _make_recipe(slots=[])

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.put(
                "/admin/templates/tmpl-123/recipe",
                headers={"X-Admin-Token": VALID_TOKEN},
                json={"recipe": recipe, "base_version_id": None},
            )

        assert res.status_code == 422

    def test_rejects_negative_duration(self, client):
        recipe = _make_recipe()
        recipe["slots"][0]["target_duration_s"] = -1.0

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.put(
                "/admin/templates/tmpl-123/recipe",
                headers={"X-Admin-Token": VALID_TOKEN},
                json={"recipe": recipe, "base_version_id": None},
            )

        assert res.status_code == 422

    def test_rejects_overlay_end_before_start(self, client):
        recipe = _make_recipe()
        recipe["slots"][0]["text_overlays"][0]["start_s"] = 5.0
        recipe["slots"][0]["text_overlays"][0]["end_s"] = 2.0

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.put(
                "/admin/templates/tmpl-123/recipe",
                headers={"X-Admin-Token": VALID_TOKEN},
                json={"recipe": recipe, "base_version_id": None},
            )

        assert res.status_code == 422

    def test_rejects_invalid_interstitial_type(self, client):
        recipe = _make_recipe()
        recipe["interstitials"][0]["type"] = "wipe-left"

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.put(
                "/admin/templates/tmpl-123/recipe",
                headers={"X-Admin-Token": VALID_TOKEN},
                json={"recipe": recipe, "base_version_id": None},
            )

        assert res.status_code == 422

    def test_conflict_when_version_changed(self, client):
        """Optimistic lock: reject if base_version_id doesn't match latest."""
        recipe = _make_recipe()
        template = _mock_template(recipe_cached=recipe)

        stale_version_id = str(uuid.uuid4())
        latest_version = _mock_version()  # Different ID

        mock_db = AsyncMock()

        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template

        lock_result = MagicMock()
        lock_result.scalar_one_or_none.return_value = latest_version

        mock_db.execute.side_effect = [template_result, lock_result]

        def _override_db():
            yield mock_db

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.put(
                    "/admin/templates/tmpl-123/recipe",
                    headers={"X-Admin-Token": VALID_TOKEN},
                    json={
                        "recipe": recipe,
                        "base_version_id": stale_version_id,
                    },
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 409
        assert "modified since" in res.json()["detail"]

    def test_auth_required(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.put(
                "/admin/templates/tmpl-123/recipe",
                json={"recipe": _make_recipe(), "base_version_id": None},
            )
        assert res.status_code in (401, 422)

    def test_rejects_invalid_slot_type(self, client):
        recipe = _make_recipe()
        recipe["slots"][0]["slot_type"] = "montage"

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.put(
                "/admin/templates/tmpl-123/recipe",
                headers={"X-Admin-Token": VALID_TOKEN},
                json={"recipe": recipe, "base_version_id": None},
            )

        assert res.status_code == 422

    def test_rejects_invalid_overlay_effect(self, client):
        recipe = _make_recipe()
        recipe["slots"][0]["text_overlays"][0]["effect"] = "explode"

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.put(
                "/admin/templates/tmpl-123/recipe",
                headers={"X-Admin-Token": VALID_TOKEN},
                json={"recipe": recipe, "base_version_id": None},
            )

        assert res.status_code == 422

    def test_rejects_invalid_hex_color(self, client):
        recipe = _make_recipe()
        recipe["slots"][0]["text_overlays"][0]["text_color"] = "banana"

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.put(
                "/admin/templates/tmpl-123/recipe",
                headers={"X-Admin-Token": VALID_TOKEN},
                json={"recipe": recipe, "base_version_id": None},
            )

        assert res.status_code == 422

    def test_rejects_zero_speed_factor(self, client):
        recipe = _make_recipe()
        recipe["slots"][0]["speed_factor"] = 0

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.put(
                "/admin/templates/tmpl-123/recipe",
                headers={"X-Admin-Token": VALID_TOKEN},
                json={"recipe": recipe, "base_version_id": None},
            )

        assert res.status_code == 422

    def test_rejects_negative_animate_s(self, client):
        recipe = _make_recipe()
        recipe["interstitials"][0]["animate_s"] = -1.0

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.put(
                "/admin/templates/tmpl-123/recipe",
                headers={"X-Admin-Token": VALID_TOKEN},
                json={"recipe": recipe, "base_version_id": None},
            )

        assert res.status_code == 422


# ── Re-render tests ──────────────────────────────────────────────────────────


def _make_assembly_plan_with_gcs(slot_count=3):
    """Build an assembly plan with clip_gcs_path in each step."""
    return {
        "steps": [
            {
                "slot": {
                    "position": i + 1,
                    "target_duration_s": 3.0 + i,
                    "priority": 5,
                    "slot_type": "hook" if i == 0 else "broll",
                    "transition_in": "hard-cut",
                    "color_hint": "none",
                    "speed_factor": 1.0,
                    "energy": 5.0,
                    "text_overlays": [],
                },
                "clip_id": f"files/clip_{i}",
                "clip_gcs_path": f"clips/user1/clip_{i}.mp4",
                "moment": {"start_s": 0.0, "end_s": 5.0, "energy": 5.0, "description": "test"},
            }
            for i in range(slot_count)
        ],
        "output_url": "https://storage.example.com/output.mp4",
    }


def _mock_source_job(template_id="tmpl-123", assembly_plan=None, status="template_ready"):
    """Create a mock Job for re-render source."""
    j = MagicMock()
    j.id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    j.template_id = template_id
    j.status = status
    j.assembly_plan = assembly_plan or _make_assembly_plan_with_gcs()
    j.selected_platforms = ["tiktok", "instagram"]
    j.all_candidates = {"clip_paths": ["clips/user1/clip_0.mp4", "clips/user1/clip_1.mp4"]}
    return j


class TestCreateRerenderJob:
    def test_creates_rerender_job(self, client):
        """Happy path: re-render with valid source job and matching slot count."""
        recipe = _make_recipe()
        template = _mock_template(recipe_cached=recipe)
        source_job = _mock_source_job()

        mock_db = AsyncMock()

        # get_template_or_404
        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template

        # db.get(Job, ...)
        mock_db.get = AsyncMock(return_value=source_job)
        mock_db.execute = AsyncMock(return_value=template_result)

        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        def _override_db():
            yield mock_db

        with (
            patch("app.routes.admin.settings") as mock_settings,
            patch("app.routes.admin.orchestrate_template_job", create=True) as mock_task,
        ):
            mock_settings.admin_api_key = VALID_TOKEN
            mock_task.delay = MagicMock()
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.post(
                    "/admin/templates/tmpl-123/rerender-job",
                    headers={"X-Admin-Token": VALID_TOKEN},
                    json={"source_job_id": str(source_job.id)},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 201
        data = res.json()
        assert data["status"] == "queued"
        assert data["template_id"] == "tmpl-123"

    def test_404_for_wrong_template(self, client):
        """Source job belongs to a different template."""
        recipe = _make_recipe()
        template = _mock_template(recipe_cached=recipe)
        source_job = _mock_source_job(template_id="other-template")

        mock_db = AsyncMock()

        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template

        mock_db.get = AsyncMock(return_value=source_job)
        mock_db.execute = AsyncMock(return_value=template_result)

        def _override_db():
            yield mock_db

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.post(
                    "/admin/templates/tmpl-123/rerender-job",
                    headers={"X-Admin-Token": VALID_TOKEN},
                    json={"source_job_id": str(source_job.id)},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404

    def test_422_for_missing_clip_gcs_path(self, client):
        """Old assembly plan without clip_gcs_path should be rejected."""
        recipe = _make_recipe()
        template = _mock_template(recipe_cached=recipe)
        # Assembly plan WITHOUT clip_gcs_path
        old_plan = {
            "steps": [
                {"slot": {"position": i + 1}, "clip_id": f"files/clip_{i}", "moment": {}}
                for i in range(3)
            ]
        }
        source_job = _mock_source_job(assembly_plan=old_plan)

        mock_db = AsyncMock()

        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template

        mock_db.get = AsyncMock(return_value=source_job)
        mock_db.execute = AsyncMock(return_value=template_result)

        def _override_db():
            yield mock_db

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.post(
                    "/admin/templates/tmpl-123/rerender-job",
                    headers={"X-Admin-Token": VALID_TOKEN},
                    json={"source_job_id": str(source_job.id)},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 422
        assert "clip_gcs_path" in res.json()["detail"]

    def test_409_for_slot_count_mismatch(self, client):
        """Recipe has 3 slots but source job has 2 steps → 409."""
        recipe = _make_recipe()  # 3 slots
        template = _mock_template(recipe_cached=recipe)
        source_job = _mock_source_job(assembly_plan=_make_assembly_plan_with_gcs(slot_count=2))

        mock_db = AsyncMock()

        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template

        mock_db.get = AsyncMock(return_value=source_job)
        mock_db.execute = AsyncMock(return_value=template_result)

        def _override_db():
            yield mock_db

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.post(
                    "/admin/templates/tmpl-123/rerender-job",
                    headers={"X-Admin-Token": VALID_TOKEN},
                    json={"source_job_id": str(source_job.id)},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 409
        assert "Slot count changed" in res.json()["detail"]

    def test_422_for_source_job_not_ready(self, client):
        """Source job that hasn't completed should be rejected."""
        recipe = _make_recipe()
        template = _mock_template(recipe_cached=recipe)
        source_job = _mock_source_job(status="processing")

        mock_db = AsyncMock()

        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template

        mock_db.get = AsyncMock(return_value=source_job)
        mock_db.execute = AsyncMock(return_value=template_result)

        def _override_db():
            yield mock_db

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.post(
                    "/admin/templates/tmpl-123/rerender-job",
                    headers={"X-Admin-Token": VALID_TOKEN},
                    json={"source_job_id": str(source_job.id)},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 422
        assert "template_ready" in res.json()["detail"]

    def test_404_for_nonexistent_source_job(self, client):
        """Source job ID that doesn't exist → 404."""
        recipe = _make_recipe()
        template = _mock_template(recipe_cached=recipe)

        mock_db = AsyncMock()

        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template

        mock_db.get = AsyncMock(return_value=None)
        mock_db.execute = AsyncMock(return_value=template_result)

        def _override_db():
            yield mock_db

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.post(
                    "/admin/templates/tmpl-123/rerender-job",
                    headers={"X-Admin-Token": VALID_TOKEN},
                    json={"source_job_id": str(uuid.uuid4())},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404

    def test_auth_required(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.post(
                "/admin/templates/tmpl-123/rerender-job",
                json={"source_job_id": str(uuid.uuid4())},
            )
        assert res.status_code in (401, 422)

    def test_rejects_invalid_sync_style(self, client):
        recipe = _make_recipe()
        recipe["sync_style"] = "random-beats"

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.put(
                "/admin/templates/tmpl-123/recipe",
                headers={"X-Admin-Token": VALID_TOKEN},
                json={"recipe": recipe, "base_version_id": None},
            )

        assert res.status_code == 422


class TestLatestTestJob:
    def test_returns_latest_completed_test_job(self, client):
        template = _mock_template(recipe_cached=_make_recipe())

        job = MagicMock()
        job.id = uuid.uuid4()
        job.assembly_plan = {"output_url": "https://storage.example.com/output.mp4"}
        job.all_candidates = {"clip_paths": ["clips/a.mp4", "clips/b.mp4"]}
        job.created_at = datetime.now(UTC)

        mock_db = AsyncMock()

        # get_template_or_404
        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template

        # latest test job query
        job_result = MagicMock()
        job_result.scalar_one_or_none.return_value = job

        mock_db.execute.side_effect = [template_result, job_result]

        def _override_db():
            yield mock_db

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.get(
                    "/admin/templates/tmpl-123/latest-test-job",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        data = res.json()
        assert data["job_id"] == str(job.id)
        assert data["output_url"] == "https://storage.example.com/output.mp4"
        assert data["clip_paths"] == ["clips/a.mp4", "clips/b.mp4"]

    def test_404_when_no_completed_test_jobs(self, client):
        template = _mock_template(recipe_cached=_make_recipe())

        mock_db = AsyncMock()

        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template

        job_result = MagicMock()
        job_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = [template_result, job_result]

        def _override_db():
            yield mock_db

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.get(
                    "/admin/templates/tmpl-123/latest-test-job",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404
        assert "No completed test jobs" in res.json()["detail"]

    def test_returns_most_recent_not_oldest(self, client):
        """The endpoint should order by created_at DESC, returning the newest."""
        template = _mock_template(recipe_cached=_make_recipe())

        # Simulate the DB returning the most recent job (the query uses ORDER BY ... DESC LIMIT 1)
        newest_job = MagicMock()
        newest_job.id = uuid.uuid4()
        newest_job.assembly_plan = {"output_url": "https://storage.example.com/newest.mp4"}
        newest_job.all_candidates = {"clip_paths": ["clips/new.mp4"]}
        newest_job.created_at = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)

        mock_db = AsyncMock()

        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template

        job_result = MagicMock()
        job_result.scalar_one_or_none.return_value = newest_job

        mock_db.execute.side_effect = [template_result, job_result]

        def _override_db():
            yield mock_db

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.get(
                    "/admin/templates/tmpl-123/latest-test-job",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        assert res.json()["output_url"] == "https://storage.example.com/newest.mp4"

    def test_handles_null_assembly_plan_and_all_candidates(self, client):
        """When assembly_plan or all_candidates are None, return null/empty defaults."""
        template = _mock_template(recipe_cached=_make_recipe())

        job = MagicMock()
        job.id = uuid.uuid4()
        job.assembly_plan = None
        job.all_candidates = None
        job.created_at = datetime.now(UTC)

        mock_db = AsyncMock()

        template_result = MagicMock()
        template_result.scalar_one_or_none.return_value = template

        job_result = MagicMock()
        job_result.scalar_one_or_none.return_value = job

        mock_db.execute.side_effect = [template_result, job_result]

        def _override_db():
            yield mock_db

        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.get(
                    "/admin/templates/tmpl-123/latest-test-job",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        data = res.json()
        assert data["output_url"] is None
        assert data["clip_paths"] == []

    def test_auth_required(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.get("/admin/templates/tmpl-123/latest-test-job")
        assert res.status_code in (401, 422)


# ── RecipeInterstitialSchema hold_s validation ──────────────────────────────


class TestInterstitialHoldSValidator:
    """hold_s=0.0 must be accepted (beat-aligned curtain close, no gap)."""

    def test_hold_s_zero_accepted(self):
        from app.routes.admin import RecipeInterstitialSchema

        inter = RecipeInterstitialSchema(
            type="curtain-close", after_slot=5, hold_s=0.0,
            hold_color="#000000", animate_s=2.0,
        )
        assert inter.hold_s == 0.0

    def test_hold_s_positive_accepted(self):
        from app.routes.admin import RecipeInterstitialSchema

        inter = RecipeInterstitialSchema(
            type="curtain-close", after_slot=3, hold_s=1.5,
        )
        assert inter.hold_s == 1.5

    def test_hold_s_negative_rejected(self):
        from app.routes.admin import RecipeInterstitialSchema
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="hold_s must be >= 0"):
            RecipeInterstitialSchema(
                type="curtain-close", after_slot=1, hold_s=-0.5,
            )
