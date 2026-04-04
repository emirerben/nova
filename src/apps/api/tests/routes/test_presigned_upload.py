"""Tests for POST /uploads/presigned — single-file presigned URL endpoint.

Covers MIME-type normalisation (NOV-8): video/quicktime must be normalised to
video/mp4 before GCS signing so the presigned URL and the client PUT header agree.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app
from app.routes.uploads import _normalise_content_type

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_db():
    """Async DB session mock that supports add() and commit()."""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


@pytest.fixture
def client(mock_db):
    """HTTPX async client with mocked DB dependency."""
    async def _override_db():
        yield mock_db

    app.dependency_overrides[get_db] = _override_db
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    yield c
    app.dependency_overrides.clear()


def _valid_body(**overrides) -> dict:
    base = {
        "filename": "test.mp4",
        "file_size_bytes": 100_000_000,
        "duration_s": 60,
        "aspect_ratio": "16:9",
        "platforms": ["instagram", "youtube"],
        "content_type": "video/mp4",
    }
    base.update(overrides)
    return base


# ── Unit: _normalise_content_type ───────────────────────────────────────────


class TestNormaliseMimeType:
    def test_mp4_passthrough(self):
        assert _normalise_content_type("video/mp4") == "video/mp4"

    def test_quicktime_normalised(self):
        assert _normalise_content_type("video/quicktime") == "video/mp4"

    def test_empty_string_normalised(self):
        assert _normalise_content_type("") == "video/mp4"

    def test_avi_passthrough(self):
        assert _normalise_content_type("video/x-msvideo") == "video/x-msvideo"


# ── Integration: presigned endpoint normalises before GCS signing ───────────


MOCK_SIGNED_URL = "https://storage.googleapis.com/signed-url"
MOCK_GCS_PATH = "dev-user/abc/raw.mp4"


@pytest.mark.asyncio
async def test_presigned_mp4_happy_path(client):
    """video/mp4 passes through and signs correctly."""
    with patch(
        "app.routes.uploads.storage.presigned_put_url",
        return_value=(MOCK_SIGNED_URL, MOCK_GCS_PATH),
    ):
        res = await client.post("/uploads/presigned", json=_valid_body())

    assert res.status_code == 201
    data = res.json()
    assert data["upload_url"] == MOCK_SIGNED_URL
    assert data["gcs_path"] == MOCK_GCS_PATH


@pytest.mark.asyncio
async def test_presigned_quicktime_normalised_to_mp4(client):
    """video/quicktime is accepted but GCS signing uses video/mp4 (NOV-8 fix)."""
    with patch(
        "app.routes.uploads.storage.presigned_put_url",
        return_value=(MOCK_SIGNED_URL, MOCK_GCS_PATH),
    ) as mock_sign:
        res = await client.post(
            "/uploads/presigned",
            json=_valid_body(content_type="video/quicktime"),
        )

    assert res.status_code == 201
    # The critical assertion: storage was called with normalised video/mp4
    assert mock_sign.call_args.kwargs["content_type"] == "video/mp4"


@pytest.mark.asyncio
async def test_presigned_avi_passes_through(client):
    """video/x-msvideo is not normalised — passes as-is to GCS."""
    with patch(
        "app.routes.uploads.storage.presigned_put_url",
        return_value=(MOCK_SIGNED_URL, MOCK_GCS_PATH),
    ) as mock_sign:
        res = await client.post(
            "/uploads/presigned",
            json=_valid_body(content_type="video/x-msvideo"),
        )

    assert res.status_code == 201
    assert mock_sign.call_args.kwargs["content_type"] == "video/x-msvideo"


@pytest.mark.asyncio
async def test_presigned_unsupported_content_type_rejected(client):
    """Unsupported MIME → 422."""
    res = await client.post(
        "/uploads/presigned",
        json=_valid_body(content_type="application/pdf"),
    )
    assert res.status_code == 422
    assert "Unsupported content type" in res.json()["detail"]


@pytest.mark.asyncio
async def test_presigned_file_too_large(client):
    """File > 4GB → 422."""
    res = await client.post(
        "/uploads/presigned",
        json=_valid_body(file_size_bytes=5 * 1024 * 1024 * 1024),
    )
    assert res.status_code == 422
    assert "4GB" in res.json()["detail"]


@pytest.mark.asyncio
async def test_presigned_invalid_aspect_ratio(client):
    """Invalid aspect ratio → 422."""
    res = await client.post(
        "/uploads/presigned",
        json=_valid_body(aspect_ratio="4:3"),
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_presigned_gcs_failure_returns_503(client):
    """GCS signing failure → 503."""
    with patch(
        "app.routes.uploads.storage.presigned_put_url",
        side_effect=Exception("GCS unavailable"),
    ):
        res = await client.post("/uploads/presigned", json=_valid_body())

    assert res.status_code == 503
    assert "unavailable" in res.json()["detail"]
