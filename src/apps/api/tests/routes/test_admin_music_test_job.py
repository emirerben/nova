"""Tests for /admin/music-tracks/{id}/test-job and /rerender-job and /test-jobs.

These exercise validation + guard paths against a mocked DB. Pipeline-level
behavior (Celery enqueue → orchestrate_music_job) is covered separately.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture(autouse=True)
def _patch_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_TOKEN)
    from app.config import settings

    settings.admin_api_key = ADMIN_TOKEN


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _admin_headers() -> dict:
    return {"X-Admin-Token": ADMIN_TOKEN}


def _ready_unpublished_track(**overrides) -> MagicMock:
    t = MagicMock()
    t.id = "track-admin-test-001"
    t.published_at = None  # crucial: admin path must accept unpublished
    t.archived_at = None
    t.analysis_status = "ready"
    t.audio_gcs_path = "music/uuid/audio.m4a"
    t.recipe_cached = None  # beat-sync, not templated
    t.track_config = {"required_clips_min": 1, "required_clips_max": 20}
    for k, v in overrides.items():
        setattr(t, k, v)
    return t


def _override_db_returning(track=None, job=None):
    """Override get_db so the first execute() returns track, the second returns job.

    The route flow for test-job: 1 execute (load track).
    The route flow for rerender-job: 2 executes (track, then source job).
    """
    track_result = MagicMock()
    track_result.scalar_one_or_none.return_value = track

    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = job

    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = []

    mock_session = AsyncMock()
    # Cycle through results in call order
    mock_session.execute = AsyncMock(side_effect=[track_result, job_result, list_result])
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock(side_effect=lambda obj: None)

    async def _override():
        yield mock_session

    return _override, mock_session


# ── POST /admin/music-tracks/{id}/test-job ────────────────────────────────────


def test_admin_test_job_requires_admin_token(client: TestClient) -> None:
    """No X-Admin-Token → 401/422."""
    resp = client.post(
        "/admin/music-tracks/some-id/test-job",
        json={"clip_gcs_paths": ["music-uploads/u1/a.mp4"]},
    )
    assert resp.status_code in (401, 422)


def test_admin_test_job_rejects_empty_clip_list(client: TestClient) -> None:
    """Pydantic validator rejects zero clips before DB lookup."""
    resp = client.post(
        "/admin/music-tracks/some-id/test-job",
        json={"clip_gcs_paths": []},
        headers=_admin_headers(),
    )
    assert resp.status_code == 422


def test_admin_test_job_rejects_too_many_clips(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/some-id/test-job",
        json={"clip_gcs_paths": [f"c/{i}.mp4" for i in range(21)]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 422


def test_admin_test_job_accepts_unpublished_ready_track(client: TestClient) -> None:
    """Crucial admin behaviour: published_at=None must NOT 422."""
    track = _ready_unpublished_track()
    override, _session = _override_db_returning(track=track)

    new_job = MagicMock()
    new_job.id = uuid4()

    app.dependency_overrides[get_db] = override
    try:
        with (
            patch("app.routes.admin_music.Job", return_value=new_job),
            patch(
                "app.services.job_dispatch.enqueue_orchestrator",
                new_callable=AsyncMock,
            ) as mock_enqueue,
        ):
            mock_enqueue.return_value = str(new_job.id)
            resp = client.post(
                f"/admin/music-tracks/{track.id}/test-job",
                json={"clip_gcs_paths": ["music-uploads/u1/a.mp4", "music-uploads/u2/b.mp4"]},
                headers=_admin_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["music_track_id"] == track.id
    mock_enqueue.assert_awaited_once()
    # First positional arg: the Celery task. Second: job UUID.
    args = mock_enqueue.await_args.args
    assert str(args[1]) == body["job_id"]


def test_admin_test_job_rejects_analyzing_track(client: TestClient) -> None:
    """analysis_status='analyzing' → 409 (must wait for beats)."""
    track = _ready_unpublished_track(analysis_status="analyzing")
    override, _session = _override_db_returning(track=track)

    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/test-job",
            json={"clip_gcs_paths": ["music-uploads/u1/a.mp4"]},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 409
    assert "analyzing" in resp.json()["detail"].lower()


def test_admin_test_job_rejects_track_without_audio(client: TestClient) -> None:
    track = _ready_unpublished_track(audio_gcs_path=None)
    override, _session = _override_db_returning(track=track)

    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/test-job",
            json={"clip_gcs_paths": ["music-uploads/u1/a.mp4"]},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 409
    assert "re-analyze" in resp.json()["detail"].lower()


def test_admin_test_job_404_when_track_missing(client: TestClient) -> None:
    override, _session = _override_db_returning(track=None)

    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            "/admin/music-tracks/missing/test-job",
            json={"clip_gcs_paths": ["music-uploads/u1/a.mp4"]},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 404


def test_admin_test_job_rejects_clip_path_outside_allowlist(client: TestClient) -> None:
    """Arbitrary GCS paths (raw user uploads, internal artifacts) must be refused."""
    resp = client.post(
        "/admin/music-tracks/some-id/test-job",
        json={"clip_gcs_paths": ["raw-uploads/sensitive/file.mp4"]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    body = " ".join(e.get("msg", "") for e in detail) if isinstance(detail, list) else str(detail)
    assert "music-uploads/" in body


def test_admin_test_job_rejects_path_traversal(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/some-id/test-job",
        json={"clip_gcs_paths": ["music-uploads/../../etc/passwd"]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 422


def test_admin_test_job_accepts_allowlisted_prefix(client: TestClient) -> None:
    """slot-uploads/ and music-uploads/ are accepted; this verifies the validator
    runs before the DB lookup so the prefix gate is the first defense."""
    resp = client.post(
        "/admin/music-tracks/missing/test-job",
        json={"clip_gcs_paths": ["music-uploads/abc/slot.mp4"]},
        headers=_admin_headers(),
    )
    # Prefix passes; track not found in mock DB → 404 (proves we got past validation)
    assert resp.status_code in (404, 500)


def test_admin_test_job_clip_count_mismatch_for_typed_slots(client: TestClient) -> None:
    """Templated track with 2 user_upload slots rejects a 1-clip submit."""
    track = _ready_unpublished_track(
        recipe_cached={
            "slots": [
                {"slot_type": "user_upload", "position": 1, "target_duration_s": 2.0},
                {"slot_type": "user_upload", "position": 2, "target_duration_s": 2.0},
            ]
        },
    )
    override, _session = _override_db_returning(track=track)

    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/test-job",
            json={"clip_gcs_paths": ["music-uploads/u1/only.mp4"]},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422
    assert "2 upload" in resp.json()["detail"]


# ── POST /admin/music-tracks/{id}/rerender-job ────────────────────────────────


def test_admin_rerender_404_on_invalid_source_uuid(client: TestClient) -> None:
    track = _ready_unpublished_track()
    override, _session = _override_db_returning(track=track)

    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/rerender-job",
            json={"source_job_id": "not-a-uuid"},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 404


def test_admin_rerender_copies_clip_paths(client: TestClient) -> None:
    """Re-render uses the source job's clip paths verbatim and enqueues a fresh job."""
    track = _ready_unpublished_track()
    source_job = MagicMock()
    source_job.id = uuid4()
    source_job.job_type = "music"
    source_job.music_track_id = track.id
    source_job.all_candidates = {"clip_paths": ["music-uploads/u1/a.mp4", "music-uploads/u2/b.mp4"]}
    source_job.selected_platforms = ["tiktok"]

    # Custom db override: don't patch Job (the route runs select(Job) AND
    # instantiates a Job). Use a refresh side_effect to assign an id on the new
    # Job object so the response can serialize it.
    track_result = MagicMock()
    track_result.scalar_one_or_none.return_value = track
    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = source_job

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[track_result, job_result])
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    new_job_id = uuid4()

    def _set_id(obj):
        obj.id = new_job_id

    mock_session.refresh = AsyncMock(side_effect=_set_id)

    async def _override():
        yield mock_session

    app.dependency_overrides[get_db] = _override
    try:
        with patch(
            "app.services.job_dispatch.enqueue_orchestrator",
            new_callable=AsyncMock,
        ) as mock_enqueue:
            mock_enqueue.return_value = str(new_job_id)
            resp = client.post(
                f"/admin/music-tracks/{track.id}/rerender-job",
                json={"source_job_id": str(source_job.id)},
                headers=_admin_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["job_id"] == str(new_job_id)
    assert body["status"] == "queued"
    mock_enqueue.assert_awaited_once()
    assert str(mock_enqueue.await_args.args[1]) == str(new_job_id)

    # Verify the new Job was constructed with the source job's clip paths.
    added_job = mock_session.add.call_args[0][0]
    assert added_job.all_candidates == {
        "clip_paths": ["music-uploads/u1/a.mp4", "music-uploads/u2/b.mp4"]
    }
    assert added_job.music_track_id == track.id
    assert added_job.selected_platforms == ["tiktok"]


def test_admin_rerender_422_when_source_has_no_clips(client: TestClient) -> None:
    track = _ready_unpublished_track()
    source_job = MagicMock()
    source_job.id = uuid4()
    source_job.job_type = "music"
    source_job.music_track_id = track.id
    source_job.all_candidates = {}  # missing clip_paths
    source_job.selected_platforms = ["tiktok"]

    override, _session = _override_db_returning(track=track, job=source_job)

    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/rerender-job",
            json={"source_job_id": str(source_job.id)},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422
    assert "clip" in resp.json()["detail"].lower()


def test_admin_rerender_404_when_source_belongs_to_other_track(client: TestClient) -> None:
    track = _ready_unpublished_track()
    source_job = MagicMock()
    source_job.id = uuid4()
    source_job.job_type = "music"
    source_job.music_track_id = "different-track-id"
    source_job.all_candidates = {"clip_paths": ["a.mp4"]}

    override, _session = _override_db_returning(track=track, job=source_job)

    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/rerender-job",
            json={"source_job_id": str(source_job.id)},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 404


# ── GET /admin/music-tracks/{id}/test-jobs ────────────────────────────────────


def test_admin_list_test_jobs_returns_summaries(client: TestClient) -> None:
    track = _ready_unpublished_track()

    track_result = MagicMock()
    track_result.scalar_one_or_none.return_value = track

    j1 = MagicMock()
    j1.id = uuid4()
    j1.status = "music_ready"
    j1.error_detail = None
    # Post-fix row: signed URL stored verbatim.
    j1.assembly_plan = {"output_url": "https://storage.googleapis.com/bucket/output.mp4?sig=abc"}
    j1.all_candidates = {"clip_paths": ["music-uploads/u1/a.mp4", "music-uploads/u2/b.mp4"]}
    j1.created_at = datetime.now(UTC)
    j1.updated_at = datetime.now(UTC)

    j_legacy = MagicMock()
    j_legacy.id = uuid4()
    j_legacy.status = "music_ready"
    j_legacy.error_detail = None
    # Pre-fix row: stored a relative GCS path. The list endpoint must strip
    # this so the admin UI doesn't render a broken <video src="music-jobs/...">.
    j_legacy.assembly_plan = {"output_url": "music-jobs/abc/output.mp4"}
    j_legacy.all_candidates = {"clip_paths": ["music-uploads/old/a.mp4"]}
    j_legacy.created_at = datetime.now(UTC)
    j_legacy.updated_at = datetime.now(UTC)

    j2 = MagicMock()
    j2.id = uuid4()
    j2.status = "processing_failed"
    j2.error_detail = "FFmpeg exit 1"
    j2.assembly_plan = None
    j2.all_candidates = {"clip_paths": ["a.mp4"]}
    j2.created_at = datetime.now(UTC)
    j2.updated_at = datetime.now(UTC)

    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [j1, j_legacy, j2]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[track_result, list_result])

    async def _override():
        yield mock_session

    app.dependency_overrides[get_db] = _override
    try:
        resp = client.get(
            f"/admin/music-tracks/{track.id}/test-jobs",
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["jobs"]) == 3
    # Post-fix row: signed URL passes through.
    assert body["jobs"][0]["status"] == "music_ready"
    assert body["jobs"][0]["output_url"].startswith("https://")
    assert body["jobs"][0]["clip_count"] == 2
    # Legacy row: relative path is stripped to null so the UI can show
    # "rerender to view" instead of a broken <video src>.
    assert body["jobs"][1]["output_url"] is None
    # Failed job: error_detail preserved.
    assert body["jobs"][2]["error_detail"] == "FFmpeg exit 1"


def test_admin_list_test_jobs_requires_admin_token(client: TestClient) -> None:
    resp = client.get("/admin/music-tracks/some-id/test-jobs")
    assert resp.status_code in (401, 422)


# ── GET /admin/music-tracks/{id}/jobs/{job_id}/status ─────────────────────────


def test_admin_job_status_returns_status_for_matching_job(client: TestClient) -> None:
    """Admin status endpoint returns the same shape as the public one."""
    track = _ready_unpublished_track()
    job_uuid = uuid4()
    job = MagicMock()
    job.id = job_uuid
    job.status = "music_ready"
    job.job_type = "music"
    job.music_track_id = track.id
    job.assembly_plan = {"output_url": "https://signed-url.example/output.mp4"}
    job.error_detail = None
    job.created_at = datetime.now(UTC)
    job.updated_at = datetime.now(UTC)

    override, _session = _override_db_returning(track=track, job=job)

    app.dependency_overrides[get_db] = override
    try:
        resp = client.get(
            f"/admin/music-tracks/{track.id}/jobs/{job_uuid}/status",
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == str(job_uuid)
    assert body["status"] == "music_ready"
    assert body["assembly_plan"]["output_url"] == "https://signed-url.example/output.mp4"


def test_admin_job_status_404_when_job_belongs_to_other_track(
    client: TestClient,
) -> None:
    """A job that exists but belongs to a different track is invisible here."""
    track = _ready_unpublished_track()
    job_uuid = uuid4()
    other_track_job = MagicMock()
    other_track_job.id = job_uuid
    other_track_job.job_type = "music"
    other_track_job.music_track_id = "some-other-track-id"

    override, _session = _override_db_returning(track=track, job=other_track_job)

    app.dependency_overrides[get_db] = override
    try:
        resp = client.get(
            f"/admin/music-tracks/{track.id}/jobs/{job_uuid}/status",
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 404


def test_admin_job_status_requires_admin_token(client: TestClient) -> None:
    resp = client.get(f"/admin/music-tracks/some-id/jobs/{uuid4()}/status")
    assert resp.status_code in (401, 422)
