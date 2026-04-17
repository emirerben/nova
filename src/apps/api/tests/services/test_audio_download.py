"""Unit tests for app/services/audio_download.py.

All network and GCS calls are mocked — these tests never hit YouTube or GCS.
"""

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.audio_download import (
    DownloadError,
    _probe_duration,
    _raise_descriptive_error,
    download_audio_and_upload,
    is_supported_audio_url,
)


# ── is_supported_audio_url ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=abc123", True),
        ("https://youtu.be/abc123", True),
        ("https://soundcloud.com/artist/track", True),
        ("https://www.soundcloud.com/artist/track", True),
        ("https://tiktok.com/@user/video/123", False),  # not supported for audio
        ("https://example.com/audio.mp3", False),
        ("not-a-url", False),
        ("", False),
    ],
)
def test_is_supported_audio_url(url: str, expected: bool) -> None:
    assert is_supported_audio_url(url) == expected


# ── _raise_descriptive_error ──────────────────────────────────────────────────


def test_raise_descriptive_error_geo() -> None:
    with pytest.raises(DownloadError, match="geo-restrict"):
        _raise_descriptive_error("https://youtube.com/x", "not available in your country")


def test_raise_descriptive_error_rate_limit() -> None:
    with pytest.raises(DownloadError, match="rate-limit"):
        _raise_descriptive_error("https://youtube.com/x", "429 Too Many Requests")


def test_raise_descriptive_error_unavailable() -> None:
    with pytest.raises(DownloadError, match="unavailable"):
        _raise_descriptive_error("https://youtube.com/x", "This video is private")


def test_raise_descriptive_error_generic() -> None:
    with pytest.raises(DownloadError, match="Failed to download"):
        _raise_descriptive_error("https://youtube.com/x", "some other yt-dlp error")


# ── _probe_duration ───────────────────────────────────────────────────────────


def test_probe_duration_success(tmp_path: Path) -> None:
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"fake")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="183.42\n", returncode=0)
        result = _probe_duration(str(audio))

    assert result == pytest.approx(183.42)


def test_probe_duration_empty_output(tmp_path: Path) -> None:
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"fake")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", returncode=1)
        result = _probe_duration(str(audio))

    assert result is None


def test_probe_duration_exception() -> None:
    with patch("subprocess.run", side_effect=OSError("ffprobe not found")):
        result = _probe_duration("/nonexistent/audio.m4a")
    assert result is None


# ── download_audio_and_upload ────────────────────────────────────────────────


def _make_fake_audio(tmpdir: str) -> None:
    """Create a fake audio.m4a in tmpdir to simulate yt-dlp output."""
    (Path(tmpdir) / "audio.m4a").write_bytes(b"fake audio content")


def test_download_audio_and_upload_success(tmp_path: Path) -> None:
    """Happy path: yt-dlp succeeds, file uploaded to GCS, returns (gcs_path, duration, thumbnail)."""
    fake_audio = tmp_path / "audio.m4a"
    fake_audio.write_bytes(b"fake audio")

    mock_ydl = MagicMock()
    mock_ydl.__enter__ = lambda s: s
    mock_ydl.__exit__ = MagicMock(return_value=False)
    mock_ydl.extract_info.return_value = {
        "thumbnail": "https://img.youtube.com/vi/abc/0.jpg",
    }

    mock_blob = MagicMock()
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket

    with (
        patch("app.services.audio_download.yt_dlp.YoutubeDL", return_value=mock_ydl),
        patch("app.services.audio_download._get_client", return_value=mock_client),
        patch("app.services.audio_download._probe_duration", return_value=183.0),
        patch("app.services.audio_download._find_audio", return_value=fake_audio),
        patch("tempfile.TemporaryDirectory") as mock_td,
    ):
        mock_td.return_value.__enter__ = lambda s: str(tmp_path)
        mock_td.return_value.__exit__ = MagicMock(return_value=False)

        gcs_path, duration_s, thumbnail_url = download_audio_and_upload(
            "https://youtube.com/watch?v=abc"
        )

    assert gcs_path.startswith("music/")
    assert gcs_path.endswith("/audio.m4a")
    assert duration_s == pytest.approx(183.0)
    assert thumbnail_url == "https://img.youtube.com/vi/abc/0.jpg"
    mock_blob.upload_from_filename.assert_called_once()


def test_download_audio_and_upload_too_long(tmp_path: Path) -> None:
    """Tracks over MAX_AUDIO_DURATION_S (600s) are rejected."""
    fake_audio = tmp_path / "audio.m4a"
    fake_audio.write_bytes(b"fake audio")

    mock_ydl = MagicMock()
    mock_ydl.__enter__ = lambda s: s
    mock_ydl.__exit__ = MagicMock(return_value=False)
    mock_ydl.extract_info.return_value = {}

    with (
        patch("app.services.audio_download.yt_dlp.YoutubeDL", return_value=mock_ydl),
        patch("app.services.audio_download._probe_duration", return_value=650.0),
        patch("app.services.audio_download._find_audio", return_value=fake_audio),
        patch("tempfile.TemporaryDirectory") as mock_td,
    ):
        mock_td.return_value.__enter__ = lambda s: str(tmp_path)
        mock_td.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(DownloadError, match="650s"):
            download_audio_and_upload("https://youtube.com/watch?v=abc")


def test_download_audio_and_upload_ydlp_error() -> None:
    """yt-dlp DownloadError is caught and re-raised as DownloadError."""
    import yt_dlp as yt_dlp_module

    mock_ydl = MagicMock()
    mock_ydl.__enter__ = lambda s: s
    mock_ydl.__exit__ = MagicMock(return_value=False)
    mock_ydl.extract_info.side_effect = yt_dlp_module.utils.DownloadError(
        "This video is unavailable"
    )

    with (
        patch("app.services.audio_download.yt_dlp.YoutubeDL", return_value=mock_ydl),
        patch("tempfile.TemporaryDirectory") as mock_td,
    ):
        mock_td.return_value.__enter__ = lambda s: "/tmp/fake"
        mock_td.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(DownloadError, match="unavailable"):
            download_audio_and_upload("https://youtube.com/watch?v=removed")


def test_download_audio_and_upload_no_file_produced(tmp_path: Path) -> None:
    """If yt-dlp produces no output file, DownloadError is raised."""
    mock_ydl = MagicMock()
    mock_ydl.__enter__ = lambda s: s
    mock_ydl.__exit__ = MagicMock(return_value=False)
    mock_ydl.extract_info.return_value = {}

    with (
        patch("app.services.audio_download.yt_dlp.YoutubeDL", return_value=mock_ydl),
        patch("app.services.audio_download._find_audio", return_value=None),
        patch("tempfile.TemporaryDirectory") as mock_td,
    ):
        mock_td.return_value.__enter__ = lambda s: str(tmp_path)
        mock_td.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(DownloadError, match="no audio file"):
            download_audio_and_upload("https://youtube.com/watch?v=abc")
