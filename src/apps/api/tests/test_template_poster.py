"""Tests for app.services.template_poster — FFmpeg poster extraction + GCS upload.

Behaviour under test (post brightness-retry change):
- One FFmpeg subprocess per seek offset; signalstats parsed from stderr.
- Returns the first attempt whose luma_mean + luma_stddev clear thresholds.
- If every attempt is too dark, returns the brightest one (never silently emit black).
- If every FFmpeg attempt fails entirely, raises PosterExtractionError.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.services.template_poster import (
    MIN_POSTER_LUMA,
    POSTER_SEEK_ATTEMPTS_S,
    PosterExtractionError,
    extract_poster_bytes,
    generate_and_upload,
)

_FAKE_JPEG = b"\xff\xd8\xff\xe0fake-jpeg-payload\xff\xd9"


def _stats_stderr(yavg: float, ydev: float) -> bytes:
    """Mimic the stderr line signalstats emits."""
    return (
        f"[Parsed_signalstats_0 @ 0x7f] YMIN:0 YLOW:1 YAVG:{yavg} "
        f"YHIGH:255 YMAX:255 UMIN:0 UAVG:128 UMAX:255 "
        f"VMIN:0 VAVG:128 VMAX:255 YDEV:{ydev}"
    ).encode()


def _ok_attempt(yavg: float = 150.0, ydev: float = 40.0) -> MagicMock:
    result = MagicMock()
    result.returncode = 0
    result.stdout = _FAKE_JPEG
    result.stderr = _stats_stderr(yavg, ydev)
    return result


def _failed_attempt() -> MagicMock:
    result = MagicMock()
    result.returncode = 1
    result.stdout = b""
    result.stderr = b"Invalid data found at offset 0"
    return result


def test_first_seek_passes_threshold_returns_immediately():
    """Bright template: first attempt at 1.5s is well above MIN_POSTER_LUMA."""
    with patch(
        "app.services.template_poster.subprocess.run",
        return_value=_ok_attempt(yavg=180.0, ydev=50.0),
    ) as run:
        out = extract_poster_bytes("/tmp/template.mp4")
    assert out == _FAKE_JPEG
    assert run.call_count == 1
    cmd = run.call_args.args[0]
    assert "signalstats" in " ".join(cmd)
    assert f"{POSTER_SEEK_ATTEMPTS_S[0]:.3f}" in cmd


def test_fade_in_clip_falls_back_to_later_seek():
    """REGRESSION: a fade-in clip is too dark at 1.5s; extractor retries 3s and
    returns the first frame that clears the threshold."""
    dark = _ok_attempt(yavg=MIN_POSTER_LUMA - 10.0, ydev=2.0)
    bright = _ok_attempt(yavg=140.0, ydev=35.0)
    with patch(
        "app.services.template_poster.subprocess.run",
        side_effect=[dark, bright],
    ) as run:
        out = extract_poster_bytes("/tmp/template.mp4")
    assert out == _FAKE_JPEG
    assert run.call_count == 2
    second_cmd = run.call_args_list[1].args[0]
    assert f"{POSTER_SEEK_ATTEMPTS_S[1]:.3f}" in second_cmd


def test_uniformly_dark_video_returns_brightest_attempt():
    """Every attempt fails the threshold (night scene). The brightest one wins
    and a warning log fires — but we never silently emit a black frame."""
    attempts = [
        _ok_attempt(yavg=5.0, ydev=1.0),
        _ok_attempt(yavg=28.0, ydev=3.0),  # brightest
        _ok_attempt(yavg=20.0, ydev=2.0),
        _ok_attempt(yavg=15.0, ydev=2.0),
    ]
    # Pin the JPEG of the brightest attempt to a distinctive value so we can
    # assert it was the one returned.
    distinctive = b"\xff\xd8\xff\xe0brightest_attempt\xff\xd9"
    attempts[1].stdout = distinctive
    with patch(
        "app.services.template_poster.subprocess.run",
        side_effect=attempts,
    ) as run:
        out = extract_poster_bytes("/tmp/template.mp4")
    assert run.call_count == len(POSTER_SEEK_ATTEMPTS_S)
    assert out == distinctive


def test_all_attempts_fail_at_ffmpeg_level_raises():
    """Seek past end-of-video for every attempt → every FFmpeg invocation fails.
    Raise instead of returning garbage."""
    with patch(
        "app.services.template_poster.subprocess.run",
        side_effect=[_failed_attempt() for _ in POSTER_SEEK_ATTEMPTS_S],
    ):
        with pytest.raises(PosterExtractionError, match="all .* seek attempts failed"):
            extract_poster_bytes("/tmp/template.mp4")


def test_partial_failure_then_success():
    """First seek fails (seek past end), second seek succeeds. We accept the second."""
    with patch(
        "app.services.template_poster.subprocess.run",
        side_effect=[_failed_attempt(), _ok_attempt(yavg=180.0, ydev=50.0)],
    ) as run:
        out = extract_poster_bytes("/tmp/template.mp4")
    assert out == _FAKE_JPEG
    assert run.call_count == 2


def test_raises_when_signalstats_output_missing():
    """A successful FFmpeg run without YAVG/YDEV in stderr is treated as a
    failed attempt (logged), not silently used."""
    bad = MagicMock()
    bad.returncode = 0
    bad.stdout = _FAKE_JPEG
    bad.stderr = b"no stats here"
    with patch(
        "app.services.template_poster.subprocess.run",
        side_effect=[bad] * len(POSTER_SEEK_ATTEMPTS_S),
    ):
        with pytest.raises(PosterExtractionError):
            extract_poster_bytes("/tmp/template.mp4")


def test_raises_on_corrupt_non_jpeg_output():
    """If every attempt produces non-JPEG bytes, that's still a failure."""
    bad = MagicMock()
    bad.returncode = 0
    bad.stdout = b"GARBAGE_NOT_JPEG_DATA"
    bad.stderr = _stats_stderr(180.0, 50.0)
    with patch(
        "app.services.template_poster.subprocess.run",
        side_effect=[bad] * len(POSTER_SEEK_ATTEMPTS_S),
    ):
        with pytest.raises(PosterExtractionError):
            extract_poster_bytes("/tmp/template.mp4")


def test_timeout_during_first_attempt_continues_to_next():
    """A timed-out FFmpeg invocation is recorded as a failed attempt; we try
    the next seek rather than aborting the whole extraction."""
    with patch(
        "app.services.template_poster.subprocess.run",
        side_effect=[
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30),
            _ok_attempt(yavg=180.0, ydev=50.0),
        ],
    ) as run:
        out = extract_poster_bytes("/tmp/template.mp4")
    assert out == _FAKE_JPEG
    assert run.call_count == 2


def test_command_includes_safety_flags():
    """Verify -- separator and stdin=DEVNULL (defense-in-depth)."""
    with patch(
        "app.services.template_poster.subprocess.run",
        return_value=_ok_attempt(yavg=180.0, ydev=50.0),
    ) as run:
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
    """If extraction fails, generate_and_upload surfaces the typed error."""
    with patch(
        "app.services.template_poster.extract_poster_bytes",
        side_effect=PosterExtractionError("all attempts failed"),
    ):
        with pytest.raises(PosterExtractionError):
            generate_and_upload("tpl-abc", "/tmp/v.mp4")
