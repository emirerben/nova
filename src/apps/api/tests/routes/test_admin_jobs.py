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
        celery_task_id=None,
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


# ── Un-reap endpoint ─────────────────────────────────────────────────────────


class TestUnReapJobs:
    """POST /admin/jobs/un-reap restores jobs the pre-#243 reaper falsely failed.

    Empirical fingerprint (from src/apps/api/app/tasks/reaper.py:120-127):
      status='processing_failed' AND failure_reason='unknown'
      AND error_detail LIKE 'Worker died with no recovery%'
      AND assembly_plan ? 'output_url'   (JSONB key existence — proves success)
    """

    def test_requires_admin_token(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            res = client.post("/admin/jobs/un-reap")
        assert res.status_code in (401, 422)

    def test_returns_restored_ids_from_returning_clause(self, client):
        """The UPDATE ... RETURNING id surfaces every row touched."""
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN

            restored = [uuid.uuid4() for _ in range(3)]

            async def _gen():
                db = AsyncMock()
                result = MagicMock()
                result.fetchall.return_value = [(rid,) for rid in restored]
                db.execute = AsyncMock(return_value=result)
                db.commit = AsyncMock()
                yield db

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.post(
                    "/admin/jobs/un-reap",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        body = res.json()
        assert body["restored"] == 3
        assert set(body["ids"]) == {str(rid) for rid in restored}

    def test_zero_matches_returns_empty_list_idempotent(self, client):
        """Re-running after the first call: WHERE clause excludes already-restored rows."""
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN

            async def _gen():
                db = AsyncMock()
                result = MagicMock()
                result.fetchall.return_value = []
                db.execute = AsyncMock(return_value=result)
                db.commit = AsyncMock()
                yield db

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.post(
                    "/admin/jobs/un-reap",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        body = res.json()
        assert body["restored"] == 0
        assert body["ids"] == []

    def test_sql_carries_reaper_fingerprint_filters(self, client):
        """Compile the UPDATE statement and verify it includes every WHERE clause
        from the reaper fingerprint. Without these, the endpoint would
        retroactively restore jobs that genuinely failed.
        """
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN

            captured: dict = {}

            async def _gen():
                db = AsyncMock()
                result = MagicMock()
                result.fetchall.return_value = []

                async def _exec(stmt, *args, **kwargs):
                    captured["stmt"] = stmt
                    return result

                db.execute = _exec
                db.commit = AsyncMock()
                yield db

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.post(
                    "/admin/jobs/un-reap",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        sql_str = str(captured["stmt"].compile()).lower()
        # Every reaper-fingerprint filter must be present in the UPDATE.
        assert "status" in sql_str
        assert "failure_reason" in sql_str
        assert "error_detail" in sql_str
        assert "like" in sql_str  # error_detail LIKE 'Worker died...%'
        assert "assembly_plan" in sql_str  # JSONB ? 'output_url' existence check


# ── Detail endpoint ──────────────────────────────────────────────────────────


class TestJobDebug:
    @pytest.fixture(autouse=True)
    def _stub_runtime(self):
        """The detail endpoint always calls _resolve_runtime, which talks to
        Celery. Stub it across this class so we don't need a live broker."""
        from app.routes.admin_jobs import JobRuntimePayload  # noqa: PLC0415

        stub = JobRuntimePayload(state="unknown", worker=None, task_id=None, queue_position=None)
        with patch("app.routes.admin_jobs._resolve_runtime", return_value=stub) as m:
            yield m

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
                # Execute order in get_job_debug:
                #   1) select(Job)
                #   2) select(JobClip)
                #   3) select(MusicTrack)              — job.music_track_id set
                #   4) select(AgentRun) by job_id
                #   5) select(AgentRun) by music_track_id
                # template_id is None on this fixture so the template branches
                # (VideoTemplate lookup + AgentRun by template_id) are skipped.
                job_res = MagicMock()
                job_res.scalar_one_or_none.return_value = j
                clips_res = MagicMock()
                clips_res.scalars.return_value.all.return_value = []
                mt_res = MagicMock()
                mt_res.scalar_one_or_none.return_value = None
                runs_res = MagicMock()
                runs_res.scalars.return_value.all.return_value = []
                track_runs_res = MagicMock()
                track_runs_res.scalars.return_value.all.return_value = []
                db.execute = AsyncMock(
                    side_effect=[job_res, clips_res, mt_res, runs_res, track_runs_res]
                )
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
        assert body["template_agent_runs"] == []
        assert body["track_agent_runs"] == []
        assert body["job_clips"] == []
        assert body["job"]["pipeline_trace"] is None

    def test_template_and_track_agent_runs_returned(self, client):
        """When the job links to a template AND a track that both have
        analysis-time agent_runs, the debug payload returns them in their
        own arrays, separate from job-time agent_runs.
        """
        template_id = uuid.uuid4()
        music_track_id = uuid.uuid4()
        j = _job_row(
            job_type="template",
            template_id=str(template_id),
            music_track_id=str(music_track_id),
        )

        def _run_row(name: str) -> SimpleNamespace:
            return SimpleNamespace(
                id=uuid.uuid4(),
                segment_idx=None,
                agent_name=name,
                prompt_version="1",
                model="m",
                outcome="ok",
                attempts=1,
                tokens_in=10,
                tokens_out=10,
                cost_usd=None,
                latency_ms=100,
                error_message=None,
                input_json={"k": "v"},
                output_json={"answer": "y"},
                raw_text=None,
                created_at=datetime.now(UTC),
            )

        template_summary = SimpleNamespace(
            id=str(template_id),
            name="Tiki Welcome",
            analysis_status="ready",
            recipe_cached={"shot_count": 5},
            audio_gcs_path=None,
            error_detail=None,
        )
        track_summary = SimpleNamespace(
            id=str(music_track_id),
            title="Waka Waka",
            artist="Shakira",
            analysis_status="ready",
            audio_gcs_path=None,
            track_config={},
            recipe_cached=None,
            beat_timestamps_s=[],
            ai_labels=None,
            best_sections=None,
            error_detail=None,
        )

        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN

            async def _gen():
                db = AsyncMock()
                # Execute order: Job → JobClip → VideoTemplate →
                # MusicTrack → AgentRun(job_id) → AgentRun(template_id) →
                # AgentRun(music_track_id)
                job_res = MagicMock()
                job_res.scalar_one_or_none.return_value = j
                clips_res = MagicMock()
                clips_res.scalars.return_value.all.return_value = []
                tpl_res = MagicMock()
                tpl_res.scalar_one_or_none.return_value = template_summary
                mt_res = MagicMock()
                mt_res.scalar_one_or_none.return_value = track_summary
                runs_res = MagicMock()
                runs_res.scalars.return_value.all.return_value = [
                    _run_row("nova.video.clip_metadata"),
                ]
                tpl_runs_res = MagicMock()
                tpl_runs_res.scalars.return_value.all.return_value = [
                    _run_row("nova.compose.template_recipe"),
                ]
                track_runs_res = MagicMock()
                track_runs_res.scalars.return_value.all.return_value = [
                    _run_row("nova.audio.song_classifier"),
                    _run_row("nova.audio.music_matcher"),
                ]
                db.execute = AsyncMock(
                    side_effect=[
                        job_res,
                        clips_res,
                        tpl_res,
                        mt_res,
                        runs_res,
                        tpl_runs_res,
                        track_runs_res,
                    ]
                )
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
        assert [r["agent_name"] for r in body["agent_runs"]] == ["nova.video.clip_metadata"]
        assert [r["agent_name"] for r in body["template_agent_runs"]] == [
            "nova.compose.template_recipe"
        ]
        assert [r["agent_name"] for r in body["track_agent_runs"]] == [
            "nova.audio.song_classifier",
            "nova.audio.music_matcher",
        ]


# ── Cancel endpoint ──────────────────────────────────────────────────────────


class TestCancelJob:
    """POST /admin/jobs/{id}/cancel — revoke Celery task + flip status."""

    def _db_gen_for_cancel(self, job, *, rowcount: int = 1):
        async def _gen():
            db = AsyncMock()
            job_res = MagicMock()
            job_res.scalar_one_or_none.return_value = job
            update_res = MagicMock()
            update_res.rowcount = rowcount
            db.execute = AsyncMock(side_effect=[job_res, update_res])
            db.commit = AsyncMock()
            db.rollback = AsyncMock()
            yield db

        return _gen

    def test_invalid_uuid_returns_400(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN

            async def _gen():
                yield AsyncMock()

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.post(
                    "/admin/jobs/not-a-uuid/cancel",
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
                res = client.post(
                    f"/admin/jobs/{uuid.uuid4()}/cancel",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 404

    def test_terminal_status_returns_409(self, client):
        """A job in 'done' / 'template_ready' / 'processing_failed' is NOT cancellable."""
        j = _job_row(status="template_ready")
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN

            async def _gen():
                db = AsyncMock()
                job_res = MagicMock()
                job_res.scalar_one_or_none.return_value = j
                db.execute = AsyncMock(return_value=job_res)
                yield db

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.post(
                    f"/admin/jobs/{j.id}/cancel",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 409
        assert "template_ready" in res.json()["detail"]

    def test_processing_with_task_id_revokes_and_flips_status(self, client):
        task_id = str(uuid.uuid4())
        j = _job_row(status="processing", celery_task_id=task_id)
        # Patch only celery_app.control (not the whole app object) so we
        # don't pollute app.tasks.maintenance's module-level @celery_app.task
        # bindings for subsequent tests in the suite.
        from app.worker import celery_app  # noqa: PLC0415

        with (
            patch("app.routes.admin.settings") as s,
            patch.object(celery_app, "control") as mock_control,
            patch("app.tasks.maintenance.cleanup_cancelled_job") as mock_cleanup,
        ):
            s.admin_api_key = VALID_TOKEN
            mock_control.revoke = MagicMock()
            mock_cleanup.apply_async = MagicMock()
            mock_cleanup.delay = MagicMock()
            mock_celery = MagicMock()
            mock_celery.control = mock_control

            app.dependency_overrides[get_db] = self._db_gen_for_cancel(j, rowcount=1)
            try:
                res = client.post(
                    f"/admin/jobs/{j.id}/cancel",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        body = res.json()
        assert body["job_id"] == str(j.id)
        assert body["previous_status"] == "processing"
        assert body["status"] == "cancelled"
        assert body["task_id"] == task_id
        assert body["revoke_dispatched"] is True
        mock_celery.control.revoke.assert_called_once_with(
            task_id, terminate=True, signal="SIGTERM"
        )
        # 30s countdown so the still-dying worker can finish writing
        # to GCS before cleanup deletes the prefix.
        mock_cleanup.apply_async.assert_called_once_with(args=[str(j.id)], countdown=30)

    def test_null_task_id_skips_revoke_but_still_flips_status(self, client):
        """Legacy row (celery_task_id=NULL): worker is gone, revoke would no-op anyway.
        We still mark the row cancelled so it stops blocking the queue picture."""
        j = _job_row(status="processing", celery_task_id=None)
        # Patch only celery_app.control (not the whole app object) so we
        # don't pollute app.tasks.maintenance's module-level @celery_app.task
        # bindings for subsequent tests in the suite.
        from app.worker import celery_app  # noqa: PLC0415

        with (
            patch("app.routes.admin.settings") as s,
            patch.object(celery_app, "control") as mock_control,
            patch("app.tasks.maintenance.cleanup_cancelled_job") as mock_cleanup,
        ):
            s.admin_api_key = VALID_TOKEN
            mock_control.revoke = MagicMock()
            mock_cleanup.apply_async = MagicMock()
            mock_cleanup.delay = MagicMock()
            mock_celery = MagicMock()
            mock_celery.control = mock_control

            app.dependency_overrides[get_db] = self._db_gen_for_cancel(j, rowcount=1)
            try:
                res = client.post(
                    f"/admin/jobs/{j.id}/cancel",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        body = res.json()
        assert body["task_id"] is None
        assert body["revoke_dispatched"] is False
        assert body["status"] == "cancelled"
        mock_celery.control.revoke.assert_not_called()

    def test_concurrent_completion_race_returns_409(self, client):
        """SELECT saw 'processing' but UPDATE matched 0 rows — worker won the race.
        Endpoint must 409 rather than silently overwriting a terminal status."""
        j = _job_row(status="processing", celery_task_id=str(uuid.uuid4()))
        # Patch only celery_app.control (not the whole app object) so we
        # don't pollute app.tasks.maintenance's module-level @celery_app.task
        # bindings for subsequent tests in the suite.
        from app.worker import celery_app  # noqa: PLC0415

        with (
            patch("app.routes.admin.settings") as s,
            patch.object(celery_app, "control") as mock_control,
            patch("app.tasks.maintenance.cleanup_cancelled_job") as mock_cleanup,
        ):
            s.admin_api_key = VALID_TOKEN
            mock_control.revoke = MagicMock()
            mock_cleanup.apply_async = MagicMock()
            mock_cleanup.delay = MagicMock()
            mock_celery = MagicMock()
            mock_celery.control = mock_control

            app.dependency_overrides[get_db] = self._db_gen_for_cancel(j, rowcount=0)
            try:
                res = client.post(
                    f"/admin/jobs/{j.id}/cancel",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 409
        assert "terminal" in res.json()["detail"].lower()


# ── Silence-cut disable endpoint ─────────────────────────────────────────────


class TestSilenceCutDisable:
    """POST /admin/jobs/{id}/silence-cut-disable — merge the per-item opt-out
    flag into assembly_plan. Only a FULL re-render picks it up afterwards."""

    def _db_gen(self, job):
        async def _gen():
            db = AsyncMock()
            job_res = MagicMock()
            job_res.scalar_one_or_none.return_value = job
            db.execute = AsyncMock(return_value=job_res)
            db.commit = AsyncMock()
            yield db

        return _gen

    def test_invalid_uuid_returns_400(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN

            async def _gen():
                yield AsyncMock()

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.post(
                    "/admin/jobs/not-a-uuid/silence-cut-disable",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 400

    def test_missing_job_returns_404(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = self._db_gen(None)
            try:
                res = client.post(
                    f"/admin/jobs/{uuid.uuid4()}/silence-cut-disable",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 404

    def test_merges_flag_into_existing_assembly_plan(self, client):
        """Existing assembly_plan keys survive — the flag is merged, not a
        wholesale replace (a replace would drop output_url/variants)."""
        j = _job_row(assembly_plan={"output_url": "https://x/y.mp4", "variants": []})
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = self._db_gen(j)
            try:
                res = client.post(
                    f"/admin/jobs/{j.id}/silence-cut-disable",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 200
        body = res.json()
        assert body["job_id"] == str(j.id)
        assert body["silence_cut_disabled"] is True
        assert j.assembly_plan == {
            "output_url": "https://x/y.mp4",
            "variants": [],
            "silence_cut_disabled": True,
        }

    def test_none_assembly_plan_is_initialized(self, client):
        """Legacy/unstarted row with assembly_plan=NULL must not 500 — the
        route initializes the dict around the flag."""
        j = _job_row(assembly_plan=None)
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = self._db_gen(j)
            try:
                res = client.post(
                    f"/admin/jobs/{j.id}/silence-cut-disable",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 200
        assert res.json()["silence_cut_disabled"] is True
        assert j.assembly_plan == {"silence_cut_disabled": True}


# ── Queue-state endpoint ─────────────────────────────────────────────────────


class TestQueueState:
    """GET /admin/jobs/queue-state — broker-level snapshot for the summary panel."""

    def test_requires_admin_token(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            res = client.get("/admin/jobs/queue-state")
        assert res.status_code in (401, 422)

    def test_returns_snapshot_shape(self, client):
        from app.services.queue_state import QueueInfo, QueueSnapshot  # noqa: PLC0415

        with (
            patch("app.routes.admin.settings") as s,
            patch("app.routes.admin_jobs.get_queue_snapshot") as mock_snapshot,
        ):
            s.admin_api_key = VALID_TOKEN
            mock_snapshot.return_value = QueueSnapshot(
                queues=[QueueInfo(name="celery", depth=3, oldest_pending_job_id="abc")],
                active_workers=["celery@worker-1"],
                ok=True,
            )

            res = client.get(
                "/admin/jobs/queue-state",
                headers={"X-Admin-Token": VALID_TOKEN},
            )

        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["active_workers"] == ["celery@worker-1"]
        assert body["queues"] == [{"name": "celery", "depth": 3, "oldest_pending_job_id": "abc"}]

    def test_returns_not_ok_when_broker_unreachable(self, client):
        from app.services.queue_state import QueueSnapshot  # noqa: PLC0415

        with (
            patch("app.routes.admin.settings") as s,
            patch("app.routes.admin_jobs.get_queue_snapshot") as mock_snapshot,
        ):
            s.admin_api_key = VALID_TOKEN
            mock_snapshot.return_value = QueueSnapshot(queues=[], active_workers=[], ok=False)

            res = client.get(
                "/admin/jobs/queue-state",
                headers={"X-Admin-Token": VALID_TOKEN},
            )

        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is False
        assert body["queues"] == []
