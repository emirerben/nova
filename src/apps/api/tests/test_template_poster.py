"""Tests for app.services.template_poster — FFmpeg poster extraction + GCS upload."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.services.template_poster import (
    PosterExtractionError,
    extract_poster_bytes,
    generate_and_upload,
)


def _mock_subprocess_run(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def test_extract_poster_bytes_returns_jpeg_data():
    """Successful FFmpeg run returns the bytes from stdout."""
    fake_jpeg = b"\xff\xd8\xff\xe0fake-jpeg-payload\xff\xd9"
    mocked = _mock_subprocess_run(returncode=0, stdout=fake_jpeg)
    with patch("app.services.template_poster.subprocess.run", return_value=mocked) as run:
        out = extract_poster_bytes("/tmp/template.mp4")
    assert out == fake_jpeg
    cmd = run.call_args.args[0]
    assert cmd[0] == "ffmpeg"
    assert "-ss" in cmd
    assert "/tmp/template.mp4" in cmd
    assert "pipe:1" in cmd
    assert "-frames:v" in cmd


def test_extract_poster_bytes_raises_on_nonzero_exit():
    """FFmpeg failure raises PosterExtractionError with stderr context."""
    mocked = _mock_subprocess_run(returncode=1, stderr=b"Invalid data found")
    with patch("app.services.template_poster.subprocess.run", return_value=mocked):
        with pytest.raises(PosterExtractionError, match="Invalid data found"):
            extract_poster_bytes("/tmp/template.mp4")


def test_extract_poster_bytes_raises_on_empty_output():
    """FFmpeg returning success but empty stdout is also a failure."""
    mocked = _mock_subprocess_run(returncode=0, stdout=b"")
    with patch("app.services.template_poster.subprocess.run", return_value=mocked):
        with pytest.raises(PosterExtractionError):
            extract_poster_bytes("/tmp/template.mp4")


def test_extract_poster_bytes_raises_on_corrupt_non_jpeg_output():
    """A successful exit with non-JPEG bytes (corrupt encode) must fail loudly
    rather than be uploaded as a broken poster."""
    mocked = _mock_subprocess_run(returncode=0, stdout=b"GARBAGE_NOT_JPEG_DATA")
    with patch("app.services.template_poster.subprocess.run", return_value=mocked):
        with pytest.raises(PosterExtractionError, match="not a valid JPEG"):
            extract_poster_bytes("/tmp/template.mp4")


def test_extract_poster_bytes_converts_timeout_to_typed_error():
    """subprocess.TimeoutExpired must surface as PosterExtractionError so the
    pipeline's narrow `except PosterExtractionError` clause catches it. Letting
    TimeoutExpired escape would crash the analyze_template Celery task."""
    with patch(
        "app.services.template_poster.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30),
    ):
        with pytest.raises(PosterExtractionError, match="timed out"):
            extract_poster_bytes("/tmp/template.mp4")


def test_extract_poster_bytes_command_includes_safety_flags():
    """Verify -- separator and stdin=DEVNULL (defense-in-depth against arg
    injection and stdin hangs)."""
    fake_jpeg = b"\xff\xd8\xff\xe0payload"
    mocked = _mock_subprocess_run(returncode=0, stdout=fake_jpeg)
    with patch("app.services.template_poster.subprocess.run", return_value=mocked) as run:
        extract_poster_bytes("/tmp/template.mp4")
    cmd = run.call_args.args[0]
    assert "--" in cmd, "missing -- separator before pipe:1"
    assert run.call_args.kwargs.get("stdin") == subprocess.DEVNULL


def test_generate_and_upload_uses_template_id_in_path():
    """The GCS object path is templates/<id>/poster.jpg."""
    fake_jpeg = b"\xff\xd8\xff\xe0payload\xff\xd9"
    with (
        patch(
            "app.services.template_poster.extract_poster_bytes",
            return_value=fake_jpeg,
        ),
        patch(
            "app.services.template_poster.upload_bytes_public_read",
        ) as upload_mock,
    ):
        upload_mock.return_value = "https://example.com/signed-url"
        gcs_path = generate_and_upload("tpl-abc", "/tmp/v.mp4")

    assert gcs_path == "templates/tpl-abc/poster.jpg"
    upload_mock.assert_called_once()
    call_args = upload_mock.call_args
    assert call_args.args[0] == fake_jpeg
    assert call_args.args[1] == "templates/tpl-abc/poster.jpg"
    assert call_args.kwargs.get("content_type") == "image/jpeg"


def test_generate_and_upload_propagates_extraction_error():
    """If FFmpeg fails the error reaches the caller (so the worker can log
    it without trying to upload garbage)."""
    with patch(
        "app.services.template_poster.extract_poster_bytes",
        side_effect=PosterExtractionError("ffmpeg returned 1"),
    ):
        with pytest.raises(PosterExtractionError):
            generate_and_upload("tpl-abc", "/tmp/v.mp4")
