"""Unit tests for routes/template_jobs.py — template job creation and status."""

import copy
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import VideoTemplate
from app.routes import template_jobs
from app.routes.template_jobs import reroll_template_job
from app.services import template_upload_promotion


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


def _promotion_job(user_id: uuid.UUID, job_id: uuid.UUID, staged: list[str]):
    journal = template_upload_promotion.build_template_upload_promotion(
        staged,
        user_id=user_id,
        job_id=job_id,
        source_generations={path: f"gen-{index}" for index, path in enumerate(staged)},
    )
    return SimpleNamespace(
        id=job_id,
        user_id=user_id,
        job_type="template",
        status="importing",
        raw_storage_path=staged[0],
        all_candidates={"clip_paths": staged, "inputs": {"location": "Tokyo"}},
        assembly_plan={template_upload_promotion.TEMPLATE_UPLOAD_PROMOTION_FIELD: journal},
        created_at=datetime.now(UTC),
        failure_reason=None,
        error_detail=None,
    )


def _promotion_session(monkeypatch, job, *, commit_error: Exception | None = None):
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.one_or_none.return_value = job
    db.execute.return_value = result
    if commit_error is not None:
        db.commit.side_effect = commit_error

    @contextmanager
    def session():
        yield db

    monkeypatch.setattr(template_upload_promotion, "sync_session", session)
    return db


def test_staged_clip_journal_pins_exact_generations_and_owned_destinations() -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    staged = [
        f"staging/{user_id}/batch-a/clip_000.mp4",
        f"staging/{user_id}/batch-a/clip_001.mov",
    ]
    journal = template_upload_promotion.build_template_upload_promotion(
        staged,
        user_id=user_id,
        job_id=job_id,
        source_generations={staged[0]: "101", staged[1]: "202"},
    )

    assert [clip["destination_path"] for clip in journal["clips"]] == [
        f"users/{user_id}/jobs/{job_id}/source/clip_000.mp4",
        f"users/{user_id}/jobs/{job_id}/source/clip_001.mov",
    ]
    assert [clip["source_generation"] for clip in journal["clips"]] == ["101", "202"]


@pytest.mark.parametrize("failure_call", [1, 2])
def test_staging_promotion_resumes_after_each_interrupted_copy(
    monkeypatch,
    failure_call: int,
) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    staged = [
        f"staging/{user_id}/batch-a/clip_000.mp4",
        f"staging/{user_id}/batch-a/clip_001.mp4",
    ]
    job = _promotion_job(user_id, job_id, staged)
    db = _promotion_session(monkeypatch, job)
    calls: list[tuple[str, str, str]] = []

    def interrupted_copy(src: str, dst: str, *, source_generation: str):
        calls.append((src, dst, source_generation))
        if len(calls) == failure_call:
            raise RuntimeError("process interrupted")
        return SimpleNamespace(generation=f"dst-{source_generation}")

    delete_source = MagicMock(return_value=True)
    monkeypatch.setattr(
        template_upload_promotion.storage,
        "copy_object_generation",
        interrupted_copy,
    )
    monkeypatch.setattr(
        template_upload_promotion.storage,
        "delete_object_generation_best_effort",
        delete_source,
    )

    with pytest.raises(RuntimeError, match="process interrupted"):
        template_upload_promotion.resume_template_upload_promotion(job_id)

    assert job.status == "importing"
    db.commit.assert_not_called()
    delete_source.assert_not_called()

    monkeypatch.setattr(
        template_upload_promotion.storage,
        "copy_object_generation",
        lambda _src, _dst, *, source_generation: SimpleNamespace(
            generation=f"dst-{source_generation}"
        ),
    )
    result = template_upload_promotion.resume_template_upload_promotion(job_id)

    assert result.state == "promoted"
    assert job.status == "queued"
    assert job.all_candidates["clip_paths"] == list(result.promoted_paths)
    assert template_upload_promotion.TEMPLATE_UPLOAD_PROMOTION_FIELD not in (
        job.assembly_plan or {}
    )
    db.commit.assert_called_once()
    assert delete_source.call_count == 2


def test_staging_promotion_resumes_when_copy_succeeded_but_commit_was_lost(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    staged = [
        f"staging/{user_id}/batch-a/clip_000.mp4",
        f"staging/{user_id}/batch-a/clip_001.mp4",
    ]
    first_job = _promotion_job(user_id, job_id, staged)
    _promotion_session(monkeypatch, first_job, commit_error=RuntimeError("commit lost"))
    copy_object = MagicMock(return_value=SimpleNamespace(generation="durable-generation"))
    delete_source = MagicMock(return_value=True)
    monkeypatch.setattr(template_upload_promotion.storage, "copy_object_generation", copy_object)
    monkeypatch.setattr(
        template_upload_promotion.storage,
        "delete_object_generation_best_effort",
        delete_source,
    )

    with pytest.raises(RuntimeError, match="commit lost"):
        template_upload_promotion.resume_template_upload_promotion(job_id)
    delete_source.assert_not_called()

    # A fresh ORM row represents the rolled-back database state. Re-copying
    # the deterministic paths is safe because storage treats destination
    # precondition failure as a lost-response retry.
    retry_job = _promotion_job(user_id, job_id, staged)
    retry_db = _promotion_session(monkeypatch, retry_job)
    result = template_upload_promotion.resume_template_upload_promotion(job_id)

    assert result.state == "promoted"
    assert retry_job.status == "queued"
    retry_db.commit.assert_called_once()
    assert copy_object.call_count == 4
    assert delete_source.call_count == 2


def test_staging_promotion_terminalizes_before_source_lifecycle_expiry(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    staged = [f"staging/{user_id}/batch-a/clip_000.mp4"]
    job = _promotion_job(user_id, job_id, staged)
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    job.created_at = now - timedelta(hours=23, minutes=1)
    db = _promotion_session(monkeypatch, job)

    state = template_upload_promotion.record_template_upload_promotion_failure(
        job_id,
        error_type="NotFound",
        now=now,
    )

    assert state == "terminal"
    assert job.status == "processing_failed"
    assert job.failure_reason == "upload_promotion_failed"
    journal = job.assembly_plan[template_upload_promotion.TEMPLATE_UPLOAD_PROMOTION_FIELD]
    assert journal["attempts"] == 1
    assert journal["last_error_type"] == "NotFound"
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_create_staged_job_commits_recovery_journal_before_first_copy(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    staged_path = f"staging/{user.id}/batch-a/clip_000.mp4"
    template = _make_template(min_clips=1, max_clips=1)
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    events: list[str] = []

    async def commit():
        events.append("commit")

    db.commit.side_effect = commit
    db.refresh = AsyncMock()
    monkeypatch.setattr(template_jobs, "get_template_or_404", AsyncMock(return_value=template))
    monkeypatch.setattr(template_jobs, "require_ready", MagicMock())
    monkeypatch.setattr(template_jobs, "validate_clip_count", MagicMock())
    monkeypatch.setattr(template_jobs, "validate_clip_total_duration", MagicMock())
    monkeypatch.setattr(template_jobs, "validate_clips_processable", AsyncMock())
    monkeypatch.setattr(
        template_jobs.storage,
        "object_metadata",
        MagicMock(return_value=SimpleNamespace(generation="source-generation")),
    )
    monkeypatch.setattr(
        "app.services.creator_direction_snapshot.ensure_job_snapshot_async",
        AsyncMock(return_value={}),
    )

    def resume(job_id):
        assert events == ["commit"]
        job = db.add.call_args.args[0]
        journal = job.assembly_plan[template_upload_promotion.TEMPLATE_UPLOAD_PROMOTION_FIELD]
        assert journal["clips"][0]["source_path"] == staged_path
        assert journal["clips"][0]["source_generation"] == "source-generation"
        assert journal["clips"][0]["destination_path"].startswith(
            f"users/{user.id}/jobs/{job_id}/source/"
        )
        events.append("copy")
        return SimpleNamespace(state="promoted")

    monkeypatch.setattr(template_jobs, "resume_template_upload_promotion", resume)
    monkeypatch.setattr(
        "app.services.job_dispatch.enqueue_orchestrator",
        AsyncMock(return_value="task-id"),
    )

    response = await template_jobs.create_template_job(
        template_jobs.CreateTemplateJobRequest(
            template_id=template.id,
            clip_gcs_paths=[staged_path],
        ),
        user,
        db,
    )

    assert response.status == "queued"
    assert events == ["commit", "copy"]


@pytest.mark.asyncio
async def test_reroll_rejects_copying_another_users_template_media() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    original = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        job_type="template",
        status="template_ready",
    )
    lock_result = MagicMock()
    original_result = MagicMock()
    original_result.scalar_one_or_none.return_value = original
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[lock_result, original_result])

    with pytest.raises(HTTPException) as exc_info:
        await reroll_template_job(str(original.id), user, db)

    assert exc_info.value.status_code == 404
    db.add.assert_not_called()


class TestCreateTemplateJobValidation:
    def test_too_few_clips_in_request_returns_422(self, client):
        """The pydantic validator requires ≥1 clip at the schema level."""
        app.dependency_overrides[get_db] = _db_with_template(_make_template())
        try:
            res = client.post(
                "/template-jobs",
                json={
                    "template_id": "template-123",
                    "clip_gcs_paths": [],  # empty list
                    "selected_platforms": ["tiktok"],
                },
            )
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 422

    def test_too_many_clips_in_request_returns_422(self, client):
        """The pydantic validator caps at 20 clips."""
        app.dependency_overrides[get_db] = _db_with_template(_make_template())
        try:
            res = client.post(
                "/template-jobs",
                json={
                    "template_id": "template-123",
                    "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(25)],  # > 20
                    "selected_platforms": ["tiktok"],
                },
            )
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 422

    def test_invalid_platform_returns_422(self, client):
        res = client.post(
            "/template-jobs",
            json={
                "template_id": "template-123",
                "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],
                "selected_platforms": ["snapchat"],  # not valid
            },
        )
        assert res.status_code == 422

    def test_template_not_found_returns_404(self, client):
        app.dependency_overrides[get_db] = _db_with_template(None)
        try:
            res = client.post(
                "/template-jobs",
                json={
                    "template_id": "nonexistent",
                    "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],
                    "selected_platforms": ["tiktok"],
                },
            )
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 404

    def test_template_not_ready_returns_409(self, client):
        app.dependency_overrides[get_db] = _db_with_template(_make_template(status="analyzing"))
        try:
            res = client.post(
                "/template-jobs",
                json={
                    "template_id": "template-123",
                    "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],
                    "selected_platforms": ["tiktok"],
                },
            )
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 409
        assert "analyzing" in res.json()["detail"]

    def test_below_template_min_clips_returns_422(self, client):
        app.dependency_overrides[get_db] = _db_with_template(_make_template(min_clips=7))
        try:
            res = client.post(
                "/template-jobs",
                json={
                    "template_id": "template-123",
                    "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],  # < min 7
                    "selected_platforms": ["tiktok"],
                },
            )
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 422
        assert "7" in res.json()["detail"]

    def test_above_template_max_clips_returns_422(self, client):
        app.dependency_overrides[get_db] = _db_with_template(_make_template(max_clips=3))
        try:
            res = client.post(
                "/template-jobs",
                json={
                    "template_id": "template-123",
                    "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],  # > max 3
                    "selected_platforms": ["tiktok"],
                },
            )
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 422
        assert "3" in res.json()["detail"]


class TestCreateTemplateJobInputs:
    """Tests for the per-template `inputs` payload + required_inputs validation."""

    def _post(self, client, template, body_inputs: dict | None = None, mocker=None):
        # Stub the dispatch helper so the test never tries to broker tasks.
        # All orchestrator dispatch now routes through enqueue_orchestrator
        # (app/services/job_dispatch.py).
        if mocker is not None:
            mocker.patch(
                "app.services.job_dispatch.enqueue_orchestrator",
                new=AsyncMock(return_value="stubbed-task-id"),
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
            res = client.post(
                "/template-jobs",
                json={
                    "template_id": "template-123",
                    "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],
                    "selected_platforms": ["tiktok"],
                    "inputs": {},
                },
            )
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 422

        # Above max
        tpl = _make_template(min_clips=2, max_clips=3)
        app.dependency_overrides[get_db] = _db_with_template(tpl)
        try:
            res = client.post(
                "/template-jobs",
                json={
                    "template_id": "template-123",
                    "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],
                    "selected_platforms": ["tiktok"],
                    "inputs": {},
                },
            )
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 422

        # In range — happy path
        mocker.patch(
            "app.services.job_dispatch.enqueue_orchestrator",
            new=AsyncMock(return_value="stubbed-task-id"),
        )
        tpl = _make_template(min_clips=2, max_clips=10)
        app.dependency_overrides[get_db] = _db_with_template(tpl)
        try:
            res = client.post(
                "/template-jobs",
                json={
                    "template_id": "template-123",
                    "clip_gcs_paths": [f"gcs/clip_{i}.mp4" for i in range(5)],
                    "selected_platforms": ["tiktok"],
                    "inputs": {},
                },
            )
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

    @pytest.mark.asyncio
    async def test_debug_steps_are_public_projection_and_stored_plan_is_unchanged(self):
        job = MagicMock()
        job.id = uuid.uuid4()
        job.job_type = "template"
        job.template_id = None
        job.status = "template_ready"
        job.error_detail = None
        job.failure_reason = None
        job.assembly_plan = {
            "steps": [
                {
                    "clip_id": "clip-1",
                    "slot": {"position": 1},
                    "clip_source_instance_ids": ["private-source"],
                    "_speech_cleanup_internal": {"secret": True},
                }
            ],
            "_speech_cleanup_internal": {"terminal_pending": {"secret": True}},
        }
        stored = copy.deepcopy(job.assembly_plan)
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)

        payload = await template_jobs.get_template_job_debug(str(job.id), db=db)

        assert payload["assembly_plan"]["steps"] == [{"slot": {"position": 1}}]
        assert payload["assembly_plan"]["clips_used_unique"] == 1
        assert job.assembly_plan == stored


class TestInputsWhitespaceStrip:
    """Pydantic validator on `inputs` strips outer whitespace so " Tokyo "
    doesn't render as "  TOKYO  " in the hook overlay. Internal whitespace
    is preserved (e.g., "São Paulo" stays as-is)."""

    def test_strips_leading_and_trailing_whitespace(self):
        from app.routes.template_jobs import CreateTemplateJobRequest

        req = CreateTemplateJobRequest(
            template_id="t",
            clip_gcs_paths=["gcs/a.mp4"],
            inputs={"location": "  Tokyo  "},
        )
        assert req.inputs["location"] == "Tokyo"

    def test_preserves_internal_whitespace(self):
        from app.routes.template_jobs import CreateTemplateJobRequest

        req = CreateTemplateJobRequest(
            template_id="t",
            clip_gcs_paths=["gcs/a.mp4"],
            inputs={"location": "  São Paulo  "},
        )
        assert req.inputs["location"] == "São Paulo"

    def test_strips_tabs_and_newlines(self):
        from app.routes.template_jobs import CreateTemplateJobRequest

        req = CreateTemplateJobRequest(
            template_id="t",
            clip_gcs_paths=["gcs/a.mp4"],
            inputs={"location": "\tTokyo\n"},
        )
        assert req.inputs["location"] == "Tokyo"

    def test_all_whitespace_strips_to_empty(self):
        """Edge case: '   ' strips to ''. Downstream _validate_inputs's
        `value.strip()` check catches this for required fields."""
        from app.routes.template_jobs import CreateTemplateJobRequest

        req = CreateTemplateJobRequest(
            template_id="t",
            clip_gcs_paths=["gcs/a.mp4"],
            inputs={"location": "   "},
        )
        assert req.inputs["location"] == ""

    def test_empty_inputs_dict_passes(self):
        from app.routes.template_jobs import CreateTemplateJobRequest

        req = CreateTemplateJobRequest(
            template_id="t",
            clip_gcs_paths=["gcs/a.mp4"],
            inputs={},
        )
        assert req.inputs == {}


class TestInputsControlCharScrubbing:
    """Pydantic validator drops invisible/bidi-override characters at the
    trust boundary. Without this, a malicious user could submit
    "Tokyo<RLO>oxoT" — the right-to-left override would visually flip
    rendering so the hook displays "Tokyo" backwards. Not exploitable for
    code execution (FFmpeg uses arg arrays), but a real visual-deception
    vector. Tab and LF are deliberately preserved (sanitize_ass_text
    handles LF; tab is rare but harmless)."""

    def _build(self, value: str) -> str:
        from app.routes.template_jobs import CreateTemplateJobRequest

        req = CreateTemplateJobRequest(
            template_id="t",
            clip_gcs_paths=["gcs/a.mp4"],
            inputs={"location": value},
        )
        return req.inputs["location"]

    def test_strips_carriage_return(self):
        assert self._build("Tok\rio") == "Tokio"

    def test_strips_null_byte(self):
        assert self._build("Tok\x00yo") == "Tokyo"

    def test_strips_rtl_override(self):
        """U+202E RIGHT-TO-LEFT OVERRIDE is the bidi-disguise vector."""
        assert self._build("Tok‮yo") == "Tokyo"

    def test_strips_zero_width_space(self):
        assert self._build("Tok​yo") == "Tokyo"

    def test_strips_unicode_line_separator(self):
        assert self._build("Tokyo ") == "Tokyo"

    def test_strips_nel(self):
        """U+0085 NEXT LINE is in the C1 range."""
        assert self._build("Tok\x85yo") == "Tokyo"

    def test_preserves_diacritics(self):
        """Latin-1 supplement and other valid printable Unicode pass through."""
        assert self._build("São Paulo") == "São Paulo"

    def test_preserves_emoji(self):
        """Emoji are valid printable Unicode — must survive scrubbing."""
        assert self._build("Tokyo 🗼") == "Tokyo 🗼"

    def test_preserves_tab(self):
        """Tab is in C0 but excluded from the strip range — sanitize_ass_text
        ignores it, and stripping would surprise users who paste from
        spreadsheets."""
        assert self._build("Tok\tyo") == "Tok\tyo"

    def test_preserves_newline(self):
        """LF is preserved here; sanitize_ass_text converts it to ASS \\\\N
        line break downstream."""
        assert self._build("Tok\nyo") == "Tok\nyo"
