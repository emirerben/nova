"""Tests for the direct admin music-track upload endpoint.

Covers the strip-video-on-ingest behavior added to handle admins uploading
full YouTube .mp4 files (video+audio) into what's nominally an audio-only
endpoint. The ffprobe + `-vn -c:a copy` re-mux fires only when a video
stream is detected; pure-audio uploads pass through unchanged.

See plan: https-nova-video-vercel-app-admin-music-glittery-sketch.md
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

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


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": ADMIN_TOKEN}


def _make_db_mock() -> MagicMock:
    session = MagicMock()
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


def test_upload_strips_video_stream_and_rewrites_gcs_path_to_m4a(
    client: TestClient,
) -> None:
    """Upload of an mp4 with embedded video → ffmpeg strips video, GCS object
    is audio-only, and the stored audio_gcs_path ends in `.m4a` regardless
    of the original filename."""
    session = _make_db_mock()
    _override_db(session)

    def fake_strip(_src: str, dest: str) -> None:
        # Simulate ffmpeg writing an audio-only file at dest.
        with open(dest, "wb") as f:
            f.write(b"AUDIO_ONLY_BYTES")

    upload_args: dict[str, Any] = {}

    fake_blob = MagicMock()

    def capture_upload(path: str, content_type: str = "") -> None:
        upload_args["local_path"] = path
        upload_args["content_type"] = content_type
        with open(path, "rb") as f:
            upload_args["bytes"] = f.read()

    fake_blob.upload_from_filename = capture_upload
    fake_bucket = MagicMock()
    blob_keys: list[str] = []

    def _blob(key: str) -> Any:
        blob_keys.append(key)
        return fake_blob

    fake_bucket.blob = _blob
    fake_client = MagicMock()
    fake_client.bucket = MagicMock(return_value=fake_bucket)

    try:
        with (
            patch("app.services.audio_preprocess.has_video_stream", return_value=True),
            patch(
                "app.services.audio_preprocess.strip_video", side_effect=fake_strip
            ) as strip_mock,
            patch("app.services.audio_download.probe_duration", return_value=223.5),
            patch("app.storage._get_client", return_value=fake_client),
            patch("app.tasks.music_orchestrate.analyze_music_track_task.delay") as dispatch,
        ):
            resp = client.post(
                "/admin/music-tracks/upload",
                headers=_admin_headers(),
                files={
                    "file": ("hawai.mp4", b"FAKE_MP4_WITH_VIDEO" * 10, "video/mp4"),
                },
                data={"title": "Hawai", "artist": "Maluma"},
            )
    finally:
        _clear_db_override()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["analysis_status"] == "queued"

    strip_mock.assert_called_once()
    # The audio-only bytes from the fake ffmpeg run — not the original mp4 payload.
    assert upload_args["bytes"] == b"AUDIO_ONLY_BYTES"
    # GCS object key must end in .m4a regardless of the user filename.
    assert blob_keys and blob_keys[0].endswith(".m4a"), blob_keys
    # Track row's audio_gcs_path matches the GCS object key.
    inserted = session.add.call_args.args[0]
    assert inserted.audio_gcs_path == blob_keys[0]
    assert inserted.audio_gcs_path.endswith(".m4a")
    dispatch.assert_called_once_with(inserted.id)


def test_upload_skips_strip_when_input_is_audio_only(client: TestClient) -> None:
    """Plain .m4a upload → no ffmpeg pass, original bytes stored as-is."""
    session = _make_db_mock()
    _override_db(session)

    payload = b"FAKE_M4A_AUDIO_BYTES" * 5

    upload_args: dict[str, Any] = {}
    fake_blob = MagicMock()

    def capture_upload(path: str, content_type: str = "") -> None:
        upload_args["path"] = path
        upload_args["content_type"] = content_type
        with open(path, "rb") as f:
            upload_args["bytes"] = f.read()

    fake_blob.upload_from_filename = capture_upload
    fake_bucket = MagicMock()
    fake_bucket.blob = MagicMock(return_value=fake_blob)
    fake_client = MagicMock()
    fake_client.bucket = MagicMock(return_value=fake_bucket)

    try:
        with (
            patch("app.services.audio_preprocess.has_video_stream", return_value=False),
            patch("app.services.audio_preprocess.strip_video") as strip_mock,
            patch("app.services.audio_download.probe_duration", return_value=180.0),
            patch("app.storage._get_client", return_value=fake_client),
            patch("app.tasks.music_orchestrate.analyze_music_track_task.delay"),
        ):
            resp = client.post(
                "/admin/music-tracks/upload",
                headers=_admin_headers(),
                files={"file": ("song.m4a", payload, "audio/mp4")},
                data={"title": "Song", "artist": "Artist"},
            )
    finally:
        _clear_db_override()

    assert resp.status_code == 201, resp.text
    strip_mock.assert_not_called()
    # The original audio bytes were uploaded verbatim.
    assert upload_args["bytes"] == payload
    # Extension preserved from the original filename.
    assert upload_args["path"].endswith(".m4a")


def test_upload_returns_413_when_over_50_mb(client: TestClient) -> None:
    """51 MB upload must short-circuit at the 50 MB cap: no ffprobe, no
    ffmpeg, no GCS write, no DB row. Locks the upfront size gate so a
    future refactor (e.g. moving the size check after probe) breaks
    loudly instead of silently."""
    session = _make_db_mock()
    _override_db(session)

    payload = b"x" * (51 * 1024 * 1024)

    try:
        with (
            patch("app.services.audio_preprocess.has_video_stream") as has_video,
            patch("app.services.audio_preprocess.strip_video") as strip_mock,
            patch("app.services.audio_download.probe_duration") as probe_mock,
            patch("app.storage._get_client") as gcs_client,
            patch("app.tasks.music_orchestrate.analyze_music_track_task.delay") as dispatch,
        ):
            resp = client.post(
                "/admin/music-tracks/upload",
                headers=_admin_headers(),
                files={"file": ("big.mp4", payload, "video/mp4")},
                data={"title": "x", "artist": "y"},
            )
    finally:
        _clear_db_override()

    assert resp.status_code == 413, resp.text
    assert "too large" in resp.json()["detail"].lower()
    has_video.assert_not_called()
    strip_mock.assert_not_called()
    probe_mock.assert_not_called()
    gcs_client.assert_not_called()
    dispatch.assert_not_called()
    session.add.assert_not_called()


def test_upload_returns_422_when_ffmpeg_fails(client: TestClient) -> None:
    """Corrupt mp4 → ffmpeg fails on strip_video → 422 to admin, not 500."""
    from app.services.audio_preprocess import AudioPreprocessError

    session = _make_db_mock()
    _override_db(session)

    try:
        with (
            patch("app.services.audio_preprocess.has_video_stream", return_value=True),
            patch(
                "app.services.audio_preprocess.strip_video",
                side_effect=AudioPreprocessError("Invalid data found"),
            ),
            patch("app.services.audio_download.probe_duration", return_value=None),
        ):
            resp = client.post(
                "/admin/music-tracks/upload",
                headers=_admin_headers(),
                files={"file": ("bad.mp4", b"garbage", "video/mp4")},
                data={"title": "x", "artist": "y"},
            )
    finally:
        _clear_db_override()

    assert resp.status_code == 422, resp.text
    assert "Failed to extract audio" in resp.json()["detail"]
    # No DB write should have happened.
    session.add.assert_not_called()
