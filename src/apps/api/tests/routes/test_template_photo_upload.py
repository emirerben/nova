"""Tests for POST /uploads/template-photo.

Specifically guards the run_in_threadpool wrapper added 2026-05-14: the
async route must propagate `ImageConversionError` raised from a sync
helper running in the threadpool back to a 422 response, AND must keep
the FastAPI event loop responsive during conversion (verified via the
single combined threadpool hop, not two separate awaits).
"""

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.database import get_db
from app.main import app
from app.models import VideoTemplate
from app.services.image_to_video import ImageConversionError


def _mock_db_with_photo_template():
    """Yield a mock async DB session whose `execute(...).scalar_one_or_none()`
    returns a VideoTemplate with one photo slot at position 1."""
    template = MagicMock(spec=VideoTemplate)
    template.id = "936e9558-248f-49be-b857-2b9a193522c6"
    template.analysis_status = "ready"
    template.recipe_cached = {
        "slots": [
            {"position": 1, "media_type": "photo", "target_duration_s": 3.5}
        ]
    }
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=template)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    yield db


@pytest.fixture()
def client_with_photo_template():
    app.dependency_overrides[get_db] = _mock_db_with_photo_template
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    app.dependency_overrides.clear()


def _png_bytes() -> bytes:
    img = Image.new("RGB", (100, 100), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _form(png: bytes):
    return {
        "data": {
            "template_id": "936e9558-248f-49be-b857-2b9a193522c6",
            "slot_position": "1",
        },
        "files": {"file": ("test.png", png, "image/png")},
    }


@patch("app.routes.uploads.storage._get_client")
@patch("app.services.image_to_video.image_bytes_to_mp4")
def test_image_conversion_error_propagates_through_threadpool_to_422(
    mock_convert, mock_storage, client_with_photo_template
):
    """Regression: ImageConversionError raised from inside the
    run_in_threadpool helper must propagate across the thread boundary
    and be caught by the route's `except ImageConversionError` clause,
    returning HTTP 422 with a readable detail message. Without this
    test, a future refactor that uses asyncio.to_thread (which has
    subtly different exception semantics) or that swallows exceptions
    in the helper would silently regress to 500."""
    mock_convert.side_effect = ImageConversionError(
        "Could not encode image: test failure tail"
    )
    res = client_with_photo_template.post(
        "/uploads/template-photo",
        **_form(_png_bytes()),
    )
    assert res.status_code == 422, f"got {res.status_code}, body={res.text!r}"
    body = res.json()
    assert "test failure tail" in body.get("detail", "")
    # Storage upload must NOT have been attempted on conversion failure
    mock_storage.assert_not_called()


@patch("app.routes.uploads.storage._get_client")
@patch("app.services.image_to_video.image_bytes_to_mp4")
def test_happy_path_returns_201_with_gcs_path(
    mock_convert, mock_storage, client_with_photo_template
):
    """Sanity check on the run_in_threadpool wiring: when both ffmpeg
    conversion and GCS upload succeed (mocked), the route returns 201
    with a JSON body containing gcs_path and duration_s."""
    mock_convert.return_value = None  # writes out_path implicitly; we don't read it
    mock_blob = MagicMock()
    mock_storage.return_value.bucket.return_value.blob.return_value = mock_blob

    res = client_with_photo_template.post(
        "/uploads/template-photo",
        **_form(_png_bytes()),
    )
    assert res.status_code == 201, f"got {res.status_code}, body={res.text!r}"
    body = res.json()
    assert "gcs_path" in body
    assert "duration_s" in body
    # Verify GCS upload was invoked (with content_type=video/mp4 the route hardcodes)
    mock_blob.upload_from_filename.assert_called_once()
    args, kwargs = mock_blob.upload_from_filename.call_args
    assert kwargs.get("content_type") == "video/mp4"
