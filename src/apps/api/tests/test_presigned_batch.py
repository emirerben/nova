"""Tests for POST /presigned-urls (batch presigned upload endpoint)."""

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _valid_file(i: int = 0) -> dict:
    return {
        "filename": f"clip_{i}.mp4",
        "content_type": "video/mp4",
        "file_size_bytes": 100_000_000,
    }


@pytest.mark.asyncio
async def test_batch_presigned_happy_path(client):
    """3 valid files → 3 signed URLs returned."""
    mock_url = "https://storage.googleapis.com/signed-url"
    mock_path = "00000000/batch-abc/clip_000.mp4"

    with patch(
        "app.routes.presigned.storage.presigned_put_url",
        return_value=(mock_url, mock_path),
    ):
        res = await client.post(
            "/presigned-urls",
            json={"files": [_valid_file(i) for i in range(3)]},
        )

    assert res.status_code == 200
    data = res.json()
    assert len(data["urls"]) == 3
    assert data["urls"][0]["upload_url"] == mock_url
    assert data["urls"][0]["gcs_path"] == mock_path


@pytest.mark.asyncio
async def test_batch_presigned_empty_files(client):
    """Empty files array → 422."""
    res = await client.post("/presigned-urls", json={"files": []})
    assert res.status_code == 422
    assert "At least one file" in res.json()["detail"]


@pytest.mark.asyncio
async def test_batch_presigned_too_many_files(client):
    """>20 files → 422."""
    files = [_valid_file(i) for i in range(21)]
    res = await client.post("/presigned-urls", json={"files": files})
    assert res.status_code == 422
    assert "Maximum 20" in res.json()["detail"]


@pytest.mark.asyncio
async def test_batch_presigned_invalid_content_type(client):
    """Invalid content type → 422."""
    f = _valid_file()
    f["content_type"] = "video/x-msvideo"
    res = await client.post("/presigned-urls", json={"files": [f]})
    assert res.status_code == 422
    assert "unsupported content type" in res.json()["detail"]


@pytest.mark.asyncio
async def test_batch_presigned_file_too_large(client):
    """File > 4GB → 422."""
    f = _valid_file()
    f["file_size_bytes"] = 5 * 1024 * 1024 * 1024  # 5GB
    res = await client.post("/presigned-urls", json={"files": [f]})
    assert res.status_code == 422
    assert "exceeds 4GB" in res.json()["detail"]


@pytest.mark.asyncio
async def test_batch_presigned_zero_size(client):
    """file_size_bytes = 0 → 422."""
    f = _valid_file()
    f["file_size_bytes"] = 0
    res = await client.post("/presigned-urls", json={"files": [f]})
    assert res.status_code == 422
    assert "must be positive" in res.json()["detail"]


@pytest.mark.asyncio
async def test_batch_presigned_gcs_failure(client):
    """GCS signing failure → 503."""
    with patch("app.routes.presigned.storage.presigned_put_url", side_effect=Exception("GCS down")):
        res = await client.post("/presigned-urls", json={"files": [_valid_file()]})

    assert res.status_code == 503
    assert "unavailable" in res.json()["detail"]
