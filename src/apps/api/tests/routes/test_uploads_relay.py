"""Regression tests for the signed-URL upload relay (/uploads/relay).

Root cause it guards: browsers on origins missing from the bucket's CORS config
(any localhost) can't PUT to storage.googleapis.com — "failed to fetch" on clip /
SFX / overlay / voiceover uploads. The relay performs the PUT server-side; these
tests pin the scope validation that keeps it from being an open relay.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user_or_synthetic
from app.main import app
from app.routes.uploads import _validate_relay_url


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


UID = "11111111-2222-3333-4444-555555555555"


def _signed(path: str) -> str:
    return f"https://storage.googleapis.com/test-bucket/{path}?X-Goog-Signature=abc"


@patch("app.routes.uploads.settings")
def test_validate_accepts_own_user_prefix(mock_settings) -> None:
    mock_settings.storage_bucket = "test-bucket"
    _validate_relay_url(_signed(f"users/{UID}/plan/i/pool/f.png"), UID)  # no raise


@patch("app.routes.uploads.settings")
def test_validate_rejects_foreign_user_prefix(mock_settings) -> None:
    from fastapi import HTTPException

    mock_settings.storage_bucket = "test-bucket"
    with pytest.raises(HTTPException) as exc:
        _validate_relay_url(_signed("users/other-user/clip.mp4"), UID)
    assert exc.value.status_code == 422


@patch("app.routes.uploads.settings")
def test_validate_rejects_wrong_host_and_bucket(mock_settings) -> None:
    from fastapi import HTTPException

    mock_settings.storage_bucket = "test-bucket"
    with pytest.raises(HTTPException):
        _validate_relay_url(f"https://evil.example/test-bucket/users/{UID}/f.png", UID)
    with pytest.raises(HTTPException):
        _validate_relay_url(
            f"https://storage.googleapis.com/other-bucket/users/{UID}/f.png", UID
        )
    with pytest.raises(HTTPException):
        _validate_relay_url(
            f"http://storage.googleapis.com/test-bucket/users/{UID}/f.png", UID
        )


def test_relay_streams_to_signed_url(client: TestClient) -> None:
    user = _user()
    app.dependency_overrides[get_current_user_or_synthetic] = lambda: user

    upstream = MagicMock()
    upstream.status_code = 200
    async_client = AsyncMock()
    async_client.__aenter__ = AsyncMock(return_value=async_client)
    async_client.__aexit__ = AsyncMock(return_value=False)
    async_client.put = AsyncMock(return_value=upstream)

    signed = (
        f"https://storage.googleapis.com/test-bucket/users/{user.id}/plan/i/clip.mp4"
        "?X-Goog-Signature=abc"
    )
    with (
        patch("app.routes.uploads.settings") as mock_settings,
        patch("httpx.AsyncClient", return_value=async_client),
    ):
        mock_settings.storage_bucket = "test-bucket"
        resp = client.post(
            "/uploads/relay",
            files={"file": ("clip.mp4", b"video-bytes", "video/mp4")},
            data={"signed_url": signed, "content_type": "video/mp4"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    put_call = async_client.put.await_args
    assert put_call.args[0] == signed
    assert put_call.kwargs["headers"]["Content-Type"] == "video/mp4"


def test_relay_surfaces_storage_rejection(client: TestClient) -> None:
    user = _user()
    app.dependency_overrides[get_current_user_or_synthetic] = lambda: user

    upstream = MagicMock()
    upstream.status_code = 403
    upstream.text = "denied"
    async_client = AsyncMock()
    async_client.__aenter__ = AsyncMock(return_value=async_client)
    async_client.__aexit__ = AsyncMock(return_value=False)
    async_client.put = AsyncMock(return_value=upstream)

    signed = (
        f"https://storage.googleapis.com/test-bucket/users/{user.id}/x.mp4"
        "?X-Goog-Signature=expired"
    )
    with (
        patch("app.routes.uploads.settings") as mock_settings,
        patch("httpx.AsyncClient", return_value=async_client),
    ):
        mock_settings.storage_bucket = "test-bucket"
        resp = client.post(
            "/uploads/relay",
            files={"file": ("x.mp4", b"bytes", "video/mp4")},
            data={"signed_url": signed, "content_type": "video/mp4"},
        )
    assert resp.status_code == 502
