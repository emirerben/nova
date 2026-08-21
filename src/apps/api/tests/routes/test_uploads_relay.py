"""Regression tests for the signed-URL upload relay (/uploads/relay).

Root cause it guards: browsers on origins missing from the bucket's CORS config
(any localhost) can't PUT to storage.googleapis.com — "failed to fetch" on clip /
SFX / overlay / voiceover uploads. The relay performs the PUT server-side; these
tests pin the scope validation that keeps it from being an open relay.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user_or_synthetic
from app.main import app
from app.routes.uploads import _validate_relay_url


def _md5_b64(payload: bytes) -> str:
    digest = hashlib.md5(payload, usedforsecurity=False)
    return base64.b64encode(digest.digest()).decode("ascii")


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
def test_validate_accepts_owned_direct_generative_prefixes(mock_settings) -> None:
    mock_settings.storage_bucket = "test-bucket"
    _validate_relay_url(_signed(f"dev-user/{UID}/generative/abc123def456/clip.mov"), UID)
    _validate_relay_url(_signed(f"voiceover-uploads/direct/{UID}/abc123def456/voice.webm"), UID)


@patch("app.routes.uploads.settings")
def test_validate_rejects_foreign_direct_generative_prefixes(mock_settings) -> None:
    from fastapi import HTTPException

    mock_settings.storage_bucket = "test-bucket"
    for path in (
        "dev-user/other-user/generative/abc123def456/clip.mov",
        "voiceover-uploads/direct/other-user/abc123def456/voice.webm",
    ):
        with pytest.raises(HTTPException) as exc:
            _validate_relay_url(_signed(path), UID)
        assert exc.value.status_code == 422


@patch("app.routes.uploads.settings")
def test_validate_rejects_wrong_host_and_bucket(mock_settings) -> None:
    from fastapi import HTTPException

    mock_settings.storage_bucket = "test-bucket"
    with pytest.raises(HTTPException):
        _validate_relay_url(f"https://evil.example/test-bucket/users/{UID}/f.png", UID)
    with pytest.raises(HTTPException):
        _validate_relay_url(f"https://storage.googleapis.com/other-bucket/users/{UID}/f.png", UID)
    with pytest.raises(HTTPException):
        _validate_relay_url(f"http://storage.googleapis.com/test-bucket/users/{UID}/f.png", UID)


@patch("app.routes.uploads.settings")
def test_validate_rejects_unsigned_bucket_url(mock_settings) -> None:
    from fastapi import HTTPException

    mock_settings.storage_bucket = "test-bucket"
    with pytest.raises(HTTPException) as exc:
        _validate_relay_url(
            f"https://storage.googleapis.com/test-bucket/users/{UID}/f.png",
            UID,
        )
    assert exc.value.status_code == 422


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
            data={
                "signed_url": signed,
                "content_type": "video/mp4",
                "file_size_bytes": str(len(b"video-bytes")),
                "if_generation_match": "0",
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    put_call = async_client.put.await_args
    assert put_call.args[0] == signed
    assert put_call.kwargs["headers"]["Content-Type"] == "video/mp4"
    assert put_call.kwargs["headers"]["Content-Length"] == str(len(b"video-bytes"))
    assert put_call.kwargs["headers"]["x-goog-if-generation-match"] == "0"


def test_relay_accepts_signed_owned_url_without_proxy_auth_header(client: TestClient) -> None:
    payload = b"video-bytes"
    upstream = MagicMock(status_code=200)
    async_client = AsyncMock()
    async_client.__aenter__ = AsyncMock(return_value=async_client)
    async_client.__aexit__ = AsyncMock(return_value=False)
    async_client.put = AsyncMock(return_value=upstream)
    signed = _signed(f"users/{UID}/plan/i/clip.mp4")

    with (
        patch("app.routes.uploads.settings") as mock_settings,
        patch("httpx.AsyncClient", return_value=async_client),
    ):
        mock_settings.storage_bucket = "test-bucket"
        response = client.post(
            "/uploads/relay",
            files={"file": ("clip.mp4", payload, "video/mp4")},
            data={
                "signed_url": signed,
                "content_type": "video/mp4",
                "file_size_bytes": str(len(payload)),
                "if_generation_match": "0",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    async_client.put.assert_awaited_once()


@pytest.mark.parametrize(
    ("declared_size", "expected_status"),
    [("0", 413), (str((200 * 1024 * 1024) + 1), 413), ("1", 422)],
)
def test_relay_rejects_invalid_or_mismatched_declared_size(
    client: TestClient,
    declared_size: str,
    expected_status: int,
) -> None:
    user = _user()
    app.dependency_overrides[get_current_user_or_synthetic] = lambda: user
    signed = _signed(f"dev-user/{user.id}/generative/abc123def456/clip.mp4")

    with patch("app.routes.uploads.settings") as mock_settings:
        mock_settings.storage_bucket = "test-bucket"
        resp = client.post(
            "/uploads/relay",
            files={"file": ("clip.mp4", b"video-bytes", "video/mp4")},
            data={
                "signed_url": signed,
                "content_type": "video/mp4",
                "file_size_bytes": declared_size,
                "if_generation_match": "0",
            },
        )

    assert resp.status_code == expected_status


def test_relay_treats_ambiguous_412_as_success_when_object_matches(
    client: TestClient,
) -> None:
    from app.storage import ObjectMetadata

    user = _user()
    app.dependency_overrides[get_current_user_or_synthetic] = lambda: user
    upstream = MagicMock(status_code=412, text="precondition failed")
    async_client = AsyncMock()
    async_client.__aenter__ = AsyncMock(return_value=async_client)
    async_client.__aexit__ = AsyncMock(return_value=False)
    async_client.put = AsyncMock(return_value=upstream)
    object_path = f"dev-user/{user.id}/generative/abc123def456/clip.mp4"
    signed = _signed(object_path)

    with (
        patch("app.routes.uploads.settings") as mock_settings,
        patch("httpx.AsyncClient", return_value=async_client),
        patch(
            "app.routes.uploads.storage.object_metadata",
            return_value=ObjectMetadata(
                path=object_path,
                generation="1",
                etag="etag",
                size=len(b"video-bytes"),
                content_type="video/mp4",
                md5_hash=_md5_b64(b"video-bytes"),
            ),
        ),
    ):
        mock_settings.storage_bucket = "test-bucket"
        resp = client.post(
            "/uploads/relay",
            files={"file": ("clip.mp4", b"video-bytes", "video/mp4")},
            data={
                "signed_url": signed,
                "content_type": "video/mp4",
                "file_size_bytes": str(len(b"video-bytes")),
                "if_generation_match": "0",
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "already_uploaded": True}


def test_relay_recovers_ambiguous_412_without_explicit_size(client: TestClient) -> None:
    from app.storage import ObjectMetadata

    user = _user()
    app.dependency_overrides[get_current_user_or_synthetic] = lambda: user
    upstream = MagicMock(status_code=412, text="precondition failed")
    async_client = AsyncMock()
    async_client.__aenter__ = AsyncMock(return_value=async_client)
    async_client.__aexit__ = AsyncMock(return_value=False)
    async_client.put = AsyncMock(return_value=upstream)
    payload = b"video-bytes"
    object_path = f"dev-user/{user.id}/generative/abc123def456/clip.mp4"

    with (
        patch("app.routes.uploads.settings") as mock_settings,
        patch("httpx.AsyncClient", return_value=async_client),
        patch(
            "app.routes.uploads.storage.object_metadata",
            return_value=ObjectMetadata(
                path=object_path,
                generation="1",
                etag="etag",
                size=len(payload),
                content_type="video/mp4",
                md5_hash=_md5_b64(payload),
            ),
        ),
    ):
        mock_settings.storage_bucket = "test-bucket"
        resp = client.post(
            "/uploads/relay",
            files={"file": ("clip.mp4", payload, "video/mp4")},
            data={
                "signed_url": _signed(object_path),
                "content_type": "video/mp4",
                "if_generation_match": "0",
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "already_uploaded": True}
    assert async_client.put.await_args.kwargs["headers"]["Content-Length"] == str(len(payload))


def test_relay_rejects_ambiguous_412_when_same_size_bytes_differ(client: TestClient) -> None:
    from app.storage import ObjectMetadata

    user = _user()
    app.dependency_overrides[get_current_user_or_synthetic] = lambda: user
    upstream = MagicMock(status_code=412, text="precondition failed")
    async_client = AsyncMock()
    async_client.__aenter__ = AsyncMock(return_value=async_client)
    async_client.__aexit__ = AsyncMock(return_value=False)
    async_client.put = AsyncMock(return_value=upstream)
    payload = b"new-bytes"
    existing = b"old-bytes"
    assert len(payload) == len(existing)
    object_path = f"dev-user/{user.id}/generative/abc123def456/clip.mp4"

    with (
        patch("app.routes.uploads.settings") as mock_settings,
        patch("httpx.AsyncClient", return_value=async_client),
        patch(
            "app.routes.uploads.storage.object_metadata",
            return_value=ObjectMetadata(
                path=object_path,
                generation="1",
                etag="etag",
                size=len(existing),
                content_type="video/mp4",
                md5_hash=_md5_b64(existing),
            ),
        ),
    ):
        mock_settings.storage_bucket = "test-bucket"
        resp = client.post(
            "/uploads/relay",
            files={"file": ("clip.mp4", payload, "video/mp4")},
            data={
                "signed_url": _signed(object_path),
                "content_type": "video/mp4",
                "if_generation_match": "0",
            },
        )

    assert resp.status_code == 502


def test_relay_does_not_accept_412_when_existing_object_metadata_differs(
    client: TestClient,
) -> None:
    from app.storage import ObjectMetadata

    user = _user()
    app.dependency_overrides[get_current_user_or_synthetic] = lambda: user
    upstream = MagicMock(status_code=412, text="precondition failed")
    async_client = AsyncMock()
    async_client.__aenter__ = AsyncMock(return_value=async_client)
    async_client.__aexit__ = AsyncMock(return_value=False)
    async_client.put = AsyncMock(return_value=upstream)
    object_path = f"dev-user/{user.id}/generative/abc123def456/clip.mp4"

    with (
        patch("app.routes.uploads.settings") as mock_settings,
        patch("httpx.AsyncClient", return_value=async_client),
        patch(
            "app.routes.uploads.storage.object_metadata",
            return_value=ObjectMetadata(
                path=object_path,
                generation="1",
                etag="etag",
                size=999,
                content_type="video/mp4",
            ),
        ),
    ):
        mock_settings.storage_bucket = "test-bucket"
        resp = client.post(
            "/uploads/relay",
            files={"file": ("clip.mp4", b"video-bytes", "video/mp4")},
            data={
                "signed_url": _signed(object_path),
                "content_type": "video/mp4",
                "file_size_bytes": str(len(b"video-bytes")),
                "if_generation_match": "0",
            },
        )

    assert resp.status_code == 502


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
        f"https://storage.googleapis.com/test-bucket/users/{user.id}/x.mp4?X-Goog-Signature=expired"
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
    assert async_client.put.await_args.kwargs["headers"]["Content-Length"] == str(len(b"bytes"))
