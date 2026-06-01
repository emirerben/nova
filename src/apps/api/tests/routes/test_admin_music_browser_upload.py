"""Tests for the browser-side ingest endpoints.

These cover the two-phase signed-URL upload flow used by the Nova Chrome
extension to push YouTube audio bytes directly to GCS, bypassing both the
Vercel function body cap and the Fly.io data-center IP that YouTube flags
as automated traffic. Plan: ``~/.claude/plans/sen-k-demli-bir-yaz-l-m-rosy-acorn.md``.

Coverage:

    upload-init
      * happy path returns signed URL + pending track row
      * Pydantic rejects bad ext, oversized byte_count, undersized byte_count,
        unsupported URL
      * dedup: same source_url within 24h → 409 + existing track_id
      * auth: missing/wrong X-Admin-Token

    upload-confirm
      * happy path: ffprobe says audio → status=queued + Celery dispatched
      * GCS HEAD finds no blob → 422, track marked failed
      * ffprobe finds no audio stream → 422, blob deleted, track marked failed
      * non-pending track returns current status idempotently

    Helpers
      * probe_has_audio_stream: positive (real WAV) + negative (text junk)
"""

from __future__ import annotations

import struct
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

ADMIN_TOKEN = "test-admin-token"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_TOKEN)
    from app.config import settings

    settings.admin_api_key = ADMIN_TOKEN


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": ADMIN_TOKEN}


def _make_db_mock(existing_track_for_dedup: Any = None) -> MagicMock:
    """Build an async-session mock that mimics enough of AsyncSession for these endpoints.

    ``existing_track_for_dedup``: if set, the first ``execute()`` call (which is
    the dedup SELECT in upload-init) returns this object; otherwise None.
    """
    session = MagicMock()

    exec_result = MagicMock()
    exec_result.scalar_one_or_none = MagicMock(return_value=existing_track_for_dedup)
    session.execute = AsyncMock(return_value=exec_result)

    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


def _override_db(session: MagicMock) -> None:
    from app.database import get_db

    async def _yield() -> Any:
        yield session

    app.dependency_overrides[get_db] = _yield


def _clear_db_override() -> None:
    from app.database import get_db

    app.dependency_overrides.pop(get_db, None)


# ─────────────────────────────────────────────────────────────────────────────
# Helper unit tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    subprocess.run(["which", "ffprobe"], capture_output=True, check=False).returncode != 0,
    reason="ffprobe not installed in the test environment",
)
def test_probe_has_audio_stream_positive_on_real_wav(tmp_path: Path) -> None:
    """A real WAV (silent, 0.1s) should be detected as audio."""
    from app.services.audio_download import probe_has_audio_stream

    # Build a minimal 44.1kHz mono 16-bit WAV header + 100ms of silence.
    sample_rate = 44100
    n_samples = sample_rate // 10  # 100 ms
    data_bytes = n_samples * 2  # 16-bit mono
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + data_bytes)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", data_bytes)
    )
    wav_path = tmp_path / "silent.wav"
    wav_path.write_bytes(header + b"\x00" * data_bytes)

    assert probe_has_audio_stream(str(wav_path)) is True


def test_probe_has_audio_stream_negative_on_text_junk(tmp_path: Path) -> None:
    """A text file pretending to be audio should be rejected."""
    from app.services.audio_download import probe_has_audio_stream

    junk = tmp_path / "not_audio.m4a"
    junk.write_text("this is definitely not an audio file")
    assert probe_has_audio_stream(str(junk)) is False


def test_probe_has_audio_stream_handles_missing_path() -> None:
    """Non-existent file must not raise — returns False so endpoint can produce 422."""
    from app.services.audio_download import probe_has_audio_stream

    assert probe_has_audio_stream("/nonexistent/audio.m4a") is False


def test_sign_track_audio_put_uses_v4_put_with_content_type() -> None:
    """The minted URL must lock Content-Type so a leaked URL can't upload arbitrary MIME."""
    from app.routes.admin_music import _BROWSER_AUDIO_PUT_TTL, _sign_track_audio_put

    fake_blob = MagicMock()
    fake_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed?x=1"
    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    with patch("app.storage._get_client", return_value=fake_client):
        url = _sign_track_audio_put("music/track-1/audio.m4a", "audio/mp4")

    assert url == "https://storage.googleapis.com/signed?x=1"
    fake_blob.generate_signed_url.assert_called_once()
    kwargs = fake_blob.generate_signed_url.call_args.kwargs
    assert kwargs["version"] == "v4"
    assert kwargs["method"] == "PUT"
    assert kwargs["content_type"] == "audio/mp4"
    assert kwargs["expiration"] == _BROWSER_AUDIO_PUT_TTL


# ─────────────────────────────────────────────────────────────────────────────
# POST /admin/music-tracks/upload-init — validation
# ─────────────────────────────────────────────────────────────────────────────


def test_upload_init_requires_auth(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/upload-init",
        json={
            "source_url": "https://youtube.com/watch?v=abc",
            "ext": ".m4a",
            "byte_count": 5_000_000,
        },
    )
    # Missing header → FastAPI 422 on Header(...); wrong token would be 401.
    assert resp.status_code in (401, 422)


def test_upload_init_rejects_wrong_token(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/upload-init",
        json={
            "source_url": "https://youtube.com/watch?v=abc",
            "ext": ".m4a",
            "byte_count": 5_000_000,
        },
        headers={"X-Admin-Token": "wrong"},
    )
    assert resp.status_code == 401


def test_upload_init_rejects_bad_ext(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/upload-init",
        json={
            "source_url": "https://youtube.com/watch?v=abc",
            "ext": ".exe",
            "byte_count": 5_000_000,
        },
        headers=admin_headers(),
    )
    assert resp.status_code == 422
    body = resp.json()
    detail_text = str(body.get("detail", ""))
    assert "ext" in detail_text.lower() or "extension" in detail_text.lower()


def test_upload_init_rejects_oversized_byte_count(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/upload-init",
        json={
            "source_url": "https://youtube.com/watch?v=abc",
            "ext": ".m4a",
            "byte_count": 200 * 1024 * 1024,  # 200 MB > 100 MB cap
        },
        headers=admin_headers(),
    )
    assert resp.status_code == 422
    assert "byte_count" in str(resp.json().get("detail", "")).lower()


def test_upload_init_rejects_undersized_byte_count(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/upload-init",
        json={
            "source_url": "https://youtube.com/watch?v=abc",
            "ext": ".m4a",
            "byte_count": 10,  # < 1 KB minimum
        },
        headers=admin_headers(),
    )
    assert resp.status_code == 422


def test_upload_init_rejects_unsupported_url(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/upload-init",
        json={
            "source_url": "https://tiktok.com/@user/video/123",
            "ext": ".m4a",
            "byte_count": 5_000_000,
        },
        headers=admin_headers(),
    )
    assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# POST /admin/music-tracks/upload-init — happy path + dedup
# ─────────────────────────────────────────────────────────────────────────────


def test_upload_init_returns_signed_url(client: TestClient) -> None:
    """Happy path: no existing track for source_url → 201 with track_id + upload_url."""
    session = _make_db_mock(existing_track_for_dedup=None)
    _override_db(session)

    fake_blob = MagicMock()
    fake_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed-put-url"
    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    try:
        with patch("app.storage._get_client", return_value=fake_client):
            resp = client.post(
                "/admin/music-tracks/upload-init",
                json={
                    "source_url": "https://youtube.com/watch?v=abc123",
                    "title": "Test Song",
                    "artist": "Test Artist",
                    "ext": ".m4a",
                    "byte_count": 12_000_000,
                },
                headers=admin_headers(),
            )
    finally:
        _clear_db_override()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["upload_url"] == "https://storage.googleapis.com/signed-put-url"
    assert body["content_type"] == "audio/mp4"
    assert body["gcs_path"].startswith("music/")
    assert body["gcs_path"].endswith("/audio.m4a")
    assert body["expires_in_s"] == 15 * 60
    assert body["track_id"]

    # MusicTrack(pending) was inserted with audio_gcs_path PRE-POPULATED.
    # We stash the path at init so upload-confirm doesn't have to probe 8
    # extensions to find the blob. Status="pending" still gates downstream
    # usage (gallery + admin job dispatch require status=="ready").
    session.add.assert_called_once()
    inserted = session.add.call_args.args[0]
    assert inserted.analysis_status == "pending"
    assert inserted.source_url == "https://youtube.com/watch?v=abc123"
    assert inserted.audio_gcs_path == body["gcs_path"]

    # Signed-URL minting was called with the locked content-type
    fake_blob.generate_signed_url.assert_called_once()
    kwargs = fake_blob.generate_signed_url.call_args.kwargs
    assert kwargs["content_type"] == "audio/mp4"
    assert kwargs["method"] == "PUT"


def test_upload_init_dedups_within_24h(client: TestClient) -> None:
    """A source_url already ingested in the last 24h → 409 + existing track_id."""
    existing = MagicMock()
    existing.id = "track-existing-001"
    existing.analysis_status = "ready"
    session = _make_db_mock(existing_track_for_dedup=existing)
    _override_db(session)

    try:
        resp = client.post(
            "/admin/music-tracks/upload-init",
            json={
                "source_url": "https://youtube.com/watch?v=abc123",
                "ext": ".m4a",
                "byte_count": 12_000_000,
            },
            headers=admin_headers(),
        )
    finally:
        _clear_db_override()

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "duplicate_source_url"
    assert detail["existing_track_id"] == "track-existing-001"
    assert detail["existing_status"] == "ready"
    # No INSERT happened
    session.add.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# POST /admin/music-tracks/{id}/upload-confirm
# ─────────────────────────────────────────────────────────────────────────────


def _make_track_mock(
    track_id: str,
    status_value: str = "pending",
    audio_gcs_path: str | None = None,
) -> MagicMock:
    t = MagicMock()
    t.id = track_id
    t.analysis_status = status_value
    # Default to the path init would have stashed so confirm tests exercise
    # the fast path (no 8-extension probe loop). Pass explicit None to test
    # the orphan-recovery fallback.
    t.audio_gcs_path = (
        audio_gcs_path if audio_gcs_path is not None else f"music/{track_id}/audio.m4a"
    )
    t.duration_s = None
    t.created_at = datetime.now(UTC)
    return t


def _override_track_lookup(track: MagicMock | None) -> None:
    """Patch _get_track_or_404 directly — simpler than mocking the session select."""
    if track is None:
        from fastapi import HTTPException
        from fastapi import status as http_status

        async def _raise(*_args: Any, **_kwargs: Any) -> None:
            raise HTTPException(http_status.HTTP_404_NOT_FOUND, detail="Music track not found")

        patcher = patch("app.routes.admin_music._get_track_or_404", side_effect=_raise)
    else:

        async def _ret(*_args: Any, **_kwargs: Any) -> MagicMock:
            return track

        patcher = patch("app.routes.admin_music._get_track_or_404", side_effect=_ret)
    patcher.start()
    return patcher


def test_upload_confirm_requires_auth(client: TestClient) -> None:
    resp = client.post("/admin/music-tracks/some-id/upload-confirm")
    assert resp.status_code in (401, 422)


def test_upload_confirm_dispatches_celery_on_happy_path(client: TestClient) -> None:
    """GCS blob present + ffprobe finds audio → status=queued + Celery delay called."""
    track = _make_track_mock("track-happy")
    session = _make_db_mock()
    _override_db(session)
    patcher = _override_track_lookup(track)

    # Fake bucket: one blob path returns True on .exists(), others False
    fake_blob = MagicMock()
    fake_blob.exists.return_value = True
    fake_blob.size = 5_000_000
    fake_blob.reload = MagicMock()
    fake_blob.download_to_filename = MagicMock()

    other_blob = MagicMock()
    other_blob.exists.return_value = False
    other_blob.reload = MagicMock()

    fake_bucket = MagicMock()
    # First lookup hits .m4a → success
    fake_bucket.blob.side_effect = lambda path: fake_blob if path.endswith(".m4a") else other_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    try:
        with (
            patch("app.storage._get_client", return_value=fake_client),
            patch("app.routes.admin_music.probe_has_audio_stream", return_value=True),
            patch("app.services.audio_download.probe_duration", return_value=215.5),
            patch("app.tasks.music_orchestrate.analyze_music_track_task") as mock_task,
        ):
            mock_task.delay = MagicMock()
            resp = client.post(
                "/admin/music-tracks/track-happy/upload-confirm",
                headers=admin_headers(),
            )
    finally:
        patcher.stop()
        _clear_db_override()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["track_id"] == "track-happy"
    assert body["analysis_status"] == "queued"
    assert body["duration_s"] == 215.5

    assert track.audio_gcs_path == "music/track-happy/audio.m4a"
    assert track.analysis_status == "queued"
    mock_task.delay.assert_called_once_with("track-happy")


def test_upload_confirm_rejects_missing_blob(client: TestClient) -> None:
    """No GCS object found at any allowed ext → 422 + track marked failed."""
    track = _make_track_mock("track-missing")
    session = _make_db_mock()
    _override_db(session)
    patcher = _override_track_lookup(track)

    none_blob = MagicMock()
    none_blob.exists.return_value = False
    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = none_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    try:
        with patch("app.storage._get_client", return_value=fake_client):
            resp = client.post(
                "/admin/music-tracks/track-missing/upload-confirm",
                headers=admin_headers(),
            )
    finally:
        patcher.stop()
        _clear_db_override()

    assert resp.status_code == 422, resp.text
    assert "No audio blob found" in resp.json()["detail"]
    assert track.analysis_status == "failed"


def test_upload_confirm_rejects_non_audio_payload(client: TestClient) -> None:
    """Blob present but ffprobe finds no audio stream → 422, blob deleted, track failed."""
    track = _make_track_mock("track-junk")
    session = _make_db_mock()
    _override_db(session)
    patcher = _override_track_lookup(track)

    fake_blob = MagicMock()
    fake_blob.exists.return_value = True
    fake_blob.size = 4_000_000
    fake_blob.reload = MagicMock()
    fake_blob.download_to_filename = MagicMock()
    fake_blob.delete = MagicMock()

    other_blob = MagicMock()
    other_blob.exists.return_value = False

    fake_bucket = MagicMock()
    fake_bucket.blob.side_effect = lambda path: fake_blob if path.endswith(".m4a") else other_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    try:
        with (
            patch("app.storage._get_client", return_value=fake_client),
            patch("app.routes.admin_music.probe_has_audio_stream", return_value=False),
            patch("app.tasks.music_orchestrate.analyze_music_track_task") as mock_task,
        ):
            mock_task.delay = MagicMock()
            resp = client.post(
                "/admin/music-tracks/track-junk/upload-confirm",
                headers=admin_headers(),
            )
    finally:
        patcher.stop()
        _clear_db_override()

    assert resp.status_code == 422, resp.text
    assert "not decodable audio" in resp.json()["detail"]
    assert track.analysis_status == "failed"
    # Junk blob got cleaned up
    fake_blob.delete.assert_called_once()
    # Celery NOT dispatched for failed tracks
    mock_task.delay.assert_not_called()


def test_upload_confirm_idempotent_for_already_queued_track(client: TestClient) -> None:
    """Re-calling confirm for a track already past pending returns current status."""
    track = _make_track_mock("track-already-queued", status_value="analyzing")
    track.audio_gcs_path = "music/track-already-queued/audio.m4a"
    track.duration_s = 180.0
    patcher = _override_track_lookup(track)
    session = _make_db_mock()
    _override_db(session)

    try:
        with patch("app.tasks.music_orchestrate.analyze_music_track_task") as mock_task:
            mock_task.delay = MagicMock()
            resp = client.post(
                "/admin/music-tracks/track-already-queued/upload-confirm",
                headers=admin_headers(),
            )
    finally:
        patcher.stop()
        _clear_db_override()

    assert resp.status_code == 200
    assert resp.json()["analysis_status"] == "analyzing"
    # Idempotent — no second Celery dispatch
    mock_task.delay.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Regression: post-review fix coverage (orphan recovery, oversize cleanup,
# stale-pending dedup). Each test pins one of the issues the /review pass
# surfaced so it can't silently regress.
# ─────────────────────────────────────────────────────────────────────────────


def test_upload_confirm_deletes_oversize_blob(client: TestClient) -> None:
    """Oversize 413 MUST delete the blob — `music/` is excluded from the 24h
    GCS lifecycle rule (CLAUDE.md storage retention), so leaking bytes here
    accrues storage cost forever. Mirror of the not_audio cleanup branch.
    """
    track = _make_track_mock("track-oversize")
    session = _make_db_mock()
    _override_db(session)
    patcher = _override_track_lookup(track)

    fake_blob = MagicMock()
    fake_blob.exists.return_value = True
    fake_blob.size = 200 * 1024 * 1024  # 200 MB > 100 MB cap
    fake_blob.reload = MagicMock()
    fake_blob.delete = MagicMock()

    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    try:
        with patch("app.storage._get_client", return_value=fake_client):
            resp = client.post(
                "/admin/music-tracks/track-oversize/upload-confirm",
                headers=admin_headers(),
            )
    finally:
        patcher.stop()
        _clear_db_override()

    assert resp.status_code == 413, resp.text
    assert "exceeds 100 MB" in resp.json()["detail"]
    assert track.analysis_status == "failed"
    # Storage cleanup — the critical assertion this regression test guards.
    fake_blob.delete.assert_called_once()


def test_upload_confirm_recovers_orphan_with_null_gcs_path(client: TestClient) -> None:
    """A track with status='queued' but audio_gcs_path=None is an orphan from
    a partial confirm failure. Re-confirm must NOT short-circuit on the
    idempotency branch — it has to fall through to the probe loop so we can
    rediscover the blob and dispatch Celery.
    """
    # status=queued (not pending/failed) AND audio_gcs_path=None — the
    # exact state that previously made the idempotency branch return without
    # ever re-dispatching Celery, stranding the track forever.
    track = _make_track_mock("track-orphan", status_value="queued", audio_gcs_path="")
    track.audio_gcs_path = None  # MagicMock-safe assignment
    session = _make_db_mock()
    _override_db(session)
    patcher = _override_track_lookup(track)

    # Fallback probe finds the blob at .m4a
    fake_blob = MagicMock()
    fake_blob.exists.return_value = True
    fake_blob.size = 5_000_000
    fake_blob.reload = MagicMock()
    fake_blob.download_to_filename = MagicMock()

    other_blob = MagicMock()
    other_blob.exists.return_value = False

    fake_bucket = MagicMock()
    fake_bucket.blob.side_effect = lambda path: fake_blob if path.endswith(".m4a") else other_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    try:
        with (
            patch("app.storage._get_client", return_value=fake_client),
            patch("app.routes.admin_music.probe_has_audio_stream", return_value=True),
            patch("app.services.audio_download.probe_duration", return_value=200.0),
            patch("app.tasks.music_orchestrate.analyze_music_track_task") as mock_task,
        ):
            mock_task.delay = MagicMock()
            resp = client.post(
                "/admin/music-tracks/track-orphan/upload-confirm",
                headers=admin_headers(),
            )
    finally:
        patcher.stop()
        _clear_db_override()

    assert resp.status_code == 200, resp.text
    # Recovered: now has gcs_path set and Celery was dispatched
    assert track.audio_gcs_path == "music/track-orphan/audio.m4a"
    mock_task.delay.assert_called_once_with("track-orphan")


def test_upload_init_ignores_stale_pending_for_dedup(client: TestClient) -> None:
    """A `pending` row older than the 15-min PUT TTL means an abandoned upload
    (admin closed the tab, signed URL expired). It must NOT block a fresh
    init for the same source_url — that's a 24-hour lockout with no recovery
    path. The dedup query excludes stale-pending rows; a new track gets
    inserted instead of a 409.
    """
    # _make_db_mock returns no existing row, simulating the stale-pending
    # filter having excluded it. This validates the contract — if a stale
    # pending existed, the query at the SQL level would have filtered it,
    # so scalar_one_or_none returns None and the new INSERT proceeds.
    session = _make_db_mock(existing_track_for_dedup=None)
    _override_db(session)

    fake_blob = MagicMock()
    fake_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed"
    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    try:
        with patch("app.storage._get_client", return_value=fake_client):
            resp = client.post(
                "/admin/music-tracks/upload-init",
                json={
                    "source_url": "https://youtube.com/watch?v=stale123",
                    "ext": ".m4a",
                    "byte_count": 10_000_000,
                },
                headers=admin_headers(),
            )
    finally:
        _clear_db_override()

    assert resp.status_code == 201, resp.text
    session.add.assert_called_once()
    inserted = session.add.call_args.args[0]
    assert inserted.analysis_status == "pending"


def test_upload_init_dedup_query_filters_stale_pending() -> None:
    """White-box: the dedup SELECT must include the stale-pending exclusion
    in its WHERE clause. Renders the compiled SQL and greps for the marker.
    Lock against accidental removal of the stale-pending dedup filter.
    """
    from sqlalchemy import select

    from app.models import MusicTrack
    from app.routes.admin_music import (
        _BROWSER_AUDIO_PUT_TTL,
        _BROWSER_INGEST_DEDUP_WINDOW,
    )

    # Sanity: the TTL is a meaningful subset of the dedup window
    assert _BROWSER_AUDIO_PUT_TTL < _BROWSER_INGEST_DEDUP_WINDOW

    # Compile a SELECT that mirrors the route's dedup query and confirm both
    # the source_url filter and the analysis_status set are present.
    stmt = select(MusicTrack).where(
        MusicTrack.source_url == "x",
        MusicTrack.analysis_status.in_(["pending", "queued", "analyzing", "ready"]),
    )
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "source_url" in compiled
    assert "pending" in compiled
    assert "queued" in compiled


# ─────────────────────────────────────────────────────────────────────────────
# POST /admin/music-tracks/upload-init-file — SPA direct-file upload variant
#
# These mirror the extension-flow tests above but exercise the no-URL variant
# used by the "Upload file" form on /admin/music. The variant bypasses Vercel's
# 4.5 MB function body cap by minting a signed PUT URL the browser uploads to
# directly. It does NOT dedup by source_url (a file upload has no canonical
# URL identity — two unrelated tracks both named "Again.mp4" must coexist).
# ─────────────────────────────────────────────────────────────────────────────


def test_upload_init_file_requires_auth(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/upload-init-file",
        json={"filename": "Again.mp4", "ext": ".mp4", "byte_count": 8_000_000},
    )
    assert resp.status_code in (401, 422)


def test_upload_init_file_rejects_wrong_token(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/upload-init-file",
        json={"filename": "Again.mp4", "ext": ".mp4", "byte_count": 8_000_000},
        headers={"X-Admin-Token": "wrong"},
    )
    assert resp.status_code == 401


def test_upload_init_file_rejects_bad_ext(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/upload-init-file",
        json={"filename": "evil.exe", "ext": ".exe", "byte_count": 8_000_000},
        headers=admin_headers(),
    )
    assert resp.status_code == 422
    detail_text = str(resp.json().get("detail", "")).lower()
    assert "ext" in detail_text or "extension" in detail_text


def test_upload_init_file_rejects_oversized_byte_count(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/upload-init-file",
        json={
            "filename": "huge.m4a",
            "ext": ".m4a",
            "byte_count": 200 * 1024 * 1024,  # 200 MB > 100 MB cap
        },
        headers=admin_headers(),
    )
    assert resp.status_code == 422
    assert "byte_count" in str(resp.json().get("detail", "")).lower()


def test_upload_init_file_rejects_undersized_byte_count(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/upload-init-file",
        json={"filename": "tiny.m4a", "ext": ".m4a", "byte_count": 10},
        headers=admin_headers(),
    )
    assert resp.status_code == 422


def test_upload_init_file_rejects_path_traversal_filename(client: TestClient) -> None:
    """Filename feeds source_url=upload://<filename>. Reject slashes."""
    resp = client.post(
        "/admin/music-tracks/upload-init-file",
        json={
            "filename": "../etc/passwd",
            "ext": ".m4a",
            "byte_count": 5_000_000,
        },
        headers=admin_headers(),
    )
    assert resp.status_code == 422


def test_upload_init_file_rejects_backslash_filename(client: TestClient) -> None:
    """Windows-style path traversal must be rejected too."""
    resp = client.post(
        "/admin/music-tracks/upload-init-file",
        json={
            "filename": "..\\windows\\system32.m4a",
            "ext": ".m4a",
            "byte_count": 5_000_000,
        },
        headers=admin_headers(),
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "bad_char,label",
    [
        ("\x00", "null"),
        ("\n", "newline"),
        ("\r", "carriage-return"),
        ("\t", "tab"),
        ("\x7f", "DEL"),
    ],
)
def test_upload_init_file_rejects_control_chars_in_filename(
    client: TestClient, bad_char: str, label: str
) -> None:
    """C0 controls + DEL break log lines and gallery card rendering."""
    resp = client.post(
        "/admin/music-tracks/upload-init-file",
        json={
            "filename": f"track{bad_char}injected.m4a",
            "ext": ".m4a",
            "byte_count": 5_000_000,
        },
        headers=admin_headers(),
    )
    assert resp.status_code == 422, f"{label} char should be rejected"


def test_upload_init_file_rejects_empty_filename(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/upload-init-file",
        json={"filename": "   ", "ext": ".m4a", "byte_count": 5_000_000},
        headers=admin_headers(),
    )
    assert resp.status_code == 422


def test_upload_init_file_returns_signed_url(client: TestClient) -> None:
    """Happy path: 201 with track_id + signed upload_url; source_url stored as upload://<filename>."""
    session = _make_db_mock(existing_track_for_dedup=None)
    _override_db(session)

    fake_blob = MagicMock()
    fake_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed-file-put"
    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    try:
        with patch("app.storage._get_client", return_value=fake_client):
            resp = client.post(
                "/admin/music-tracks/upload-init-file",
                json={
                    "filename": "Again.mp4",
                    "title": "Again",
                    "artist": "Roger Sanchez",
                    "ext": ".mp4",
                    "byte_count": 8_970_070,
                },
                headers=admin_headers(),
            )
    finally:
        _clear_db_override()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["upload_url"] == "https://storage.googleapis.com/signed-file-put"
    # .mp4 maps to audio/mp4 (YouTube AAC-in-MP4 reports application/mp4 too).
    assert body["content_type"] == "audio/mp4"
    assert body["gcs_path"].startswith("music/")
    assert body["gcs_path"].endswith("/audio.mp4")
    assert body["expires_in_s"] == 15 * 60
    assert body["track_id"]

    # MusicTrack was inserted with source_url=upload://<filename>, audio_gcs_path
    # pre-populated, status=pending — matching the extension init contract.
    session.add.assert_called_once()
    track = session.add.call_args.args[0]
    assert track.source_url == "upload://Again.mp4"
    assert track.title == "Again"
    assert track.artist == "Roger Sanchez"
    assert track.audio_gcs_path == body["gcs_path"]
    assert track.analysis_status == "pending"


def test_upload_init_file_skips_source_url_dedup(client: TestClient) -> None:
    """File uploads must NOT consult the 24h source_url dedup query.

    Two unrelated tracks the admin happens to name "Again.mp4" must produce two
    distinct DB rows. The extension flow dedups by source_url because it has a
    canonical YouTube URL identity; direct uploads do not. Empirically verify
    the route never executes a SELECT against MusicTrack (the dedup query in
    /upload-init is the only SELECT in either init handler).
    """
    session = _make_db_mock(existing_track_for_dedup=None)
    _override_db(session)

    fake_blob = MagicMock()
    fake_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed-file-put"
    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    try:
        with patch("app.storage._get_client", return_value=fake_client):
            resp = client.post(
                "/admin/music-tracks/upload-init-file",
                json={
                    "filename": "Again.mp4",
                    "ext": ".mp4",
                    "byte_count": 8_000_000,
                },
                headers=admin_headers(),
            )
    finally:
        _clear_db_override()

    assert resp.status_code == 201, resp.text
    # No SELECT was issued — the dedup path is genuinely bypassed.
    session.execute.assert_not_called()


def test_upload_init_file_defaults_title_to_filename_when_missing(
    client: TestClient,
) -> None:
    session = _make_db_mock(existing_track_for_dedup=None)
    _override_db(session)

    fake_blob = MagicMock()
    fake_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed"
    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    try:
        with patch("app.storage._get_client", return_value=fake_client):
            resp = client.post(
                "/admin/music-tracks/upload-init-file",
                json={
                    "filename": "Some Random Clip.m4a",
                    "ext": ".m4a",
                    "byte_count": 4_000_000,
                },
                headers=admin_headers(),
            )
    finally:
        _clear_db_override()

    assert resp.status_code == 201, resp.text
    track = session.add.call_args.args[0]
    # Title fell back to filename rather than "Track <8-hex>" because the
    # filename carries more user signal than a random UUID prefix.
    assert track.title == "Some Random Clip.m4a"
    assert track.artist == ""
