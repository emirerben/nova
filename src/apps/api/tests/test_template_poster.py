"""Tests for app.services.template_poster — FFmpeg poster extraction + GCS upload.

Behaviour under test (post brightness-retry change):
- One FFmpeg subprocess per seek offset; showinfo luma statistics parsed from stderr.
- Returns the first attempt whose luma_mean + luma_stddev clear thresholds.
- If every attempt is too dark, returns the brightest one (never silently emit black).
- If every FFmpeg attempt fails entirely, raises PosterExtractionError.
"""

import hashlib
import shutil
import subprocess
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.services.template_poster import (
    MIN_POSTER_LUMA,
    POSTER_SEEK_ATTEMPTS_S,
    PosterExtractionError,
    _extract_attempt,
    extract_poster_bytes,
    generate_and_upload,
    generate_and_upload_from_gcs,
    upload_video_poster,
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
    assert "showinfo" in " ".join(cmd)
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
    attempts = [_ok_attempt(yavg=5.0 + i, ydev=1.0) for i in range(len(POSTER_SEEK_ATTEMPTS_S))]
    attempts[1] = _ok_attempt(yavg=28.0, ydev=3.0)  # brightest
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


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_real_ffmpeg_short_clip_emits_a_valid_poster(tmp_path):
    """Exercise the production filter chain, including showinfo statistics."""
    source = tmp_path / "short.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=25",
            "-t",
            "0.2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
    )

    poster = extract_poster_bytes(str(source))

    assert poster.startswith(b"\xff\xd8\xff")
    assert poster.endswith(b"\xff\xd9")
    assert len(poster) > 100


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


def test_video_poster_object_path_is_deterministic():
    from app.services.template_poster import poster_object_path

    assert poster_object_path("jobs/job-1/output.mp4") == "jobs/job-1/output.mp4.poster.jpg"


def test_video_poster_object_path_uses_durable_prefix_with_job_id():
    """A job-scoped poster must not inherit the video prefix's lifecycle rule."""
    from app.services.template_poster import poster_object_path

    job_id = "4bfd1ff0-3f27-4a3c-9f6c-9f8b8d1a1a11"
    key = poster_object_path("music-jobs/job-1/output.mp4", job_id=job_id)

    digest = hashlib.sha1(b"music-jobs/job-1/output.mp4").hexdigest()
    assert key == f"job-posters/{job_id}/{digest}.poster.jpg"
    assert key.endswith(".poster.jpg")


def test_video_poster_object_path_is_deterministic_per_source_object():
    """Same source ⇒ same key, so a renderer and a repair collide benignly."""
    from app.services.template_poster import poster_object_path

    first = poster_object_path("jobs/job-1/output.mp4", job_id="job-1")
    second = poster_object_path("jobs/job-1/output.mp4", job_id="job-1")
    other_source = poster_object_path("jobs/job-1/base.mp4", job_id="job-1")
    other_job = poster_object_path("jobs/job-1/output.mp4", job_id="job-2")

    assert first == second
    assert first != other_source
    assert first != other_job

    # A UUID caller must reach the same durable key as its string form, not be
    # silently demoted to the lifecycle-bound sibling.
    job_uuid = uuid.uuid4()
    assert poster_object_path("jobs/j/output.mp4", job_id=job_uuid) == poster_object_path(
        "jobs/j/output.mp4", job_id=str(job_uuid)
    )
    assert poster_object_path("jobs/j/output.mp4", job_id=job_uuid).startswith(
        f"job-posters/{job_uuid}/"
    )


@pytest.mark.parametrize(
    "job_id",
    [None, "", "   ", "../escape", "job/nested", "job id"],
    ids=["none", "empty", "blank", "traversal", "slash", "space"],
)
def test_video_poster_object_path_falls_back_to_legacy_sibling(job_id):
    """Unusable ids keep today's sibling key instead of escaping the prefix."""
    from app.services.template_poster import poster_object_path

    assert (
        poster_object_path("jobs/job-1/output.mp4", job_id=job_id)
        == "jobs/job-1/output.mp4.poster.jpg"
    )


def test_upload_video_poster_writes_durable_key_when_job_id_is_known(monkeypatch, tmp_path):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"mp4")
    uploads: list[tuple[bytes, str]] = []
    monkeypatch.setattr("app.services.template_poster.extract_poster_bytes", lambda _: b"jpeg")
    monkeypatch.setattr(
        "app.services.template_poster.upload_bytes_public_read",
        lambda data, path, content_type: uploads.append((data, path)),
    )

    poster_path = upload_video_poster(
        str(source),
        "music-jobs/job-1/output.mp4",
        job_id="job-1",
    )

    digest = hashlib.sha1(b"music-jobs/job-1/output.mp4").hexdigest()
    assert poster_path == f"job-posters/job-1/{digest}.poster.jpg"
    assert uploads == [(b"jpeg", poster_path)]


def test_generate_and_upload_from_gcs_downloads_exact_video_key(monkeypatch):
    downloaded = {}

    def fake_download(path, local_path):
        downloaded["path"] = path
        downloaded["local_path"] = local_path

    monkeypatch.setattr("app.services.template_poster.download_to_file", fake_download)
    monkeypatch.setattr(
        "app.services.template_poster.upload_video_poster",
        lambda local_path, video_path, *, job_id=None: f"{video_path}.poster.jpg:{job_id}",
    )

    result = generate_and_upload_from_gcs("generative-jobs/job-1/output.mp4", job_id="job-1")

    # The job id must reach the key derivation, not just the log line.
    assert result == "generative-jobs/job-1/output.mp4.poster.jpg:job-1"
    assert downloaded["path"] == "generative-jobs/job-1/output.mp4"


def test_generate_and_upload_from_gcs_returns_durable_relative_key(monkeypatch):
    """Lane contract: the caller gets a relative key back, never a signed URL."""
    monkeypatch.setattr("app.services.template_poster.download_to_file", lambda *_args: None)
    monkeypatch.setattr("app.services.template_poster.os.path.isfile", lambda _path: True)
    monkeypatch.setattr("app.services.template_poster.extract_poster_bytes", lambda _: b"jpeg")
    monkeypatch.setattr(
        "app.services.template_poster.upload_bytes_public_read",
        lambda *_args, **_kwargs: "https://storage.googleapis.com/bucket/signed",
    )

    job_id = "4bfd1ff0-3f27-4a3c-9f6c-9f8b8d1a1a11"
    result = generate_and_upload_from_gcs(
        f"generative-jobs/{job_id}/output.mp4",
        job_id=job_id,
        source_kind="poster_repair",
    )

    digest = hashlib.sha1(f"generative-jobs/{job_id}/output.mp4".encode()).hexdigest()
    assert result == f"job-posters/{job_id}/{digest}.poster.jpg"


def test_durable_poster_key_is_readable_through_the_job_output_allowlist():
    """The new prefix must survive the ownership check on the read path."""
    from types import SimpleNamespace

    from app.services.job_storage_paths import owned_job_output_path
    from app.services.template_poster import poster_object_path

    job = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    key = poster_object_path(f"music-jobs/{job.id}/output.mp4", job_id=str(job.id))

    assert owned_job_output_path(key, job) == key


def test_generate_and_upload_from_gcs_is_fail_open_when_poster_extraction_fails(monkeypatch):
    monkeypatch.setattr(
        "app.services.template_poster.download_to_file",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "app.services.template_poster.upload_video_poster",
        lambda *_args: (_ for _ in ()).throw(PosterExtractionError("bad video")),
    )

    assert generate_and_upload_from_gcs("generative-jobs/job-1/output.mp4") is None


def test_extract_attempt_accepts_showinfo_luma_statistics():
    result = _ok_attempt()
    result.stderr = b"[Parsed_showinfo_1] mean:[123 128 128] stdev:[42 3 4]"
    with patch("app.services.template_poster.subprocess.run", return_value=result):
        attempt = _extract_attempt("/tmp/template.mp4", 0.5)

    assert attempt.luma_mean == 123.0
    assert attempt.luma_stddev == 42.0


def test_extract_poster_stops_after_total_budget_before_next_attempt(monkeypatch):
    times = iter([0.0, 0.0, 0.0, 91.0])
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise PosterExtractionError("ffmpeg failed")

    monkeypatch.setattr("app.services.template_poster.time.monotonic", lambda: next(times))
    monkeypatch.setattr("app.services.template_poster._extract_attempt", fail)

    with pytest.raises(PosterExtractionError, match="exceeded 90s total budget"):
        extract_poster_bytes("/tmp/template.mp4")
    assert len(calls) == 1


def test_upload_video_poster_rejects_missing_local_source(monkeypatch):
    monkeypatch.setattr("app.services.template_poster.extract_poster_bytes", lambda _: b"jpeg")

    with pytest.raises(PosterExtractionError, match="source does not exist"):
        upload_video_poster("/tmp/does-not-exist.mp4", "jobs/job/output.mp4")


def test_generate_and_upload_from_gcs_is_fail_open_on_download_error(monkeypatch):
    monkeypatch.setattr(
        "app.services.template_poster.download_to_file",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("storage unavailable")),
    )

    assert generate_and_upload_from_gcs("jobs/job/output.mp4", job_id="job") is None
