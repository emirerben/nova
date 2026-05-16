"""Unit tests for the live-eval fixture URI normalizer.

The normalizer is exactly the silent failure mode that broke v0.4.8.0's
live-eval gate: fixtures stored bucket-relative paths, the production
flow uploads them first, the eval flow forgot to. These tests fence
each leg of that path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ._fixture_uploader import (
    FixtureUploader,
    is_bucket_relative,
)

_GEMINI_FILE_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


class _FakeFileRef:
    """Mirrors the real google-genai File object shape.

    The SDK exposes both `.name` (bare `files/<id>`) and `.uri` (the full
    URL form Gemini accepts at `Part.from_uri` call time). Tests should
    assert against `.uri` because that's what the uploader returns.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.uri = f"{_GEMINI_FILE_API_BASE}/{name}"


class _Recorder:
    """Records calls to download/upload so we can assert on them."""

    def __init__(self, *, upload_id: str = "files/abc123") -> None:
        self.downloaded: list[tuple[str, str]] = []
        self.uploaded: list[str] = []
        self.upload_id = upload_id

    def download(self, object_path: str, local_path: str) -> None:
        self.downloaded.append((object_path, local_path))
        # Touch the file so the upload step would have something to read.
        Path(local_path).write_bytes(b"\x00" * 8)

    def upload(self, path: str, timeout: int = 120) -> _FakeFileRef:
        self.uploaded.append(path)
        return _FakeFileRef(self.upload_id)


def _make_uploader(rec: _Recorder) -> FixtureUploader:
    return FixtureUploader(download_fn=rec.download, upload_fn=rec.upload)


# ── is_bucket_relative ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("clips/2c750692193b.mp4", True),
        ("templates/fb30073f/video.mp4", True),
        ("audio/track-42.m4a", True),
        ("files/gjfw7iewbbwk", False),
        ("gs://nova-videos-dev/clips/x.mp4", False),
        ("https://example.com/x.mp4", False),
        ("http://example.com/x.mp4", False),
        ("", False),
    ],
)
def test_is_bucket_relative(uri: str, expected: bool) -> None:
    assert is_bucket_relative(uri) is expected


# ── normalize ────────────────────────────────────────────────────────────────


def test_normalize_passthrough_for_files_api_id() -> None:
    rec = _Recorder()
    uploader = _make_uploader(rec)

    assert uploader.normalize("files/gjfw7iewbbwk") == "files/gjfw7iewbbwk"
    assert rec.downloaded == []
    assert rec.uploaded == []


def test_normalize_passthrough_for_gs_uri() -> None:
    rec = _Recorder()
    uploader = _make_uploader(rec)

    assert uploader.normalize("gs://bucket/path.mp4") == "gs://bucket/path.mp4"
    assert rec.downloaded == []


def test_normalize_passthrough_for_https() -> None:
    rec = _Recorder()
    uploader = _make_uploader(rec)

    assert uploader.normalize("https://example.com/x.mp4") == "https://example.com/x.mp4"
    assert rec.downloaded == []


def test_normalize_uploads_bucket_relative_path() -> None:
    rec = _Recorder(upload_id="files/abc123")
    uploader = _make_uploader(rec)

    result = uploader.normalize("clips/2c750692193b.mp4")

    assert result == f"{_GEMINI_FILE_API_BASE}/files/abc123"
    assert len(rec.downloaded) == 1
    assert rec.downloaded[0][0] == "clips/2c750692193b.mp4"
    # Suffix preserved on the temp path so Gemini can sniff mime
    assert rec.downloaded[0][1].endswith(".mp4")
    assert rec.uploaded == [rec.downloaded[0][1]]


def test_normalize_caches_repeat_uploads() -> None:
    counter = {"n": 0}

    def fake_dl(_object_path: str, local_path: str) -> None:
        Path(local_path).write_bytes(b"\x00")

    def fake_up(_path: str, timeout: int = 120) -> _FakeFileRef:
        counter["n"] += 1
        return _FakeFileRef(f"files/up-{counter['n']}")

    uploader = FixtureUploader(download_fn=fake_dl, upload_fn=fake_up)

    a = uploader.normalize("clips/x.mp4")
    b = uploader.normalize("clips/x.mp4")
    c = uploader.normalize("clips/y.mp4")

    assert a == b == f"{_GEMINI_FILE_API_BASE}/files/up-1"
    assert c == f"{_GEMINI_FILE_API_BASE}/files/up-2"
    assert counter["n"] == 2  # x.mp4 uploaded once, y.mp4 once


def test_normalize_propagates_download_errors() -> None:
    def boom(_object_path: str, _local_path: str) -> None:
        raise FileNotFoundError("gcs object missing")

    uploader = FixtureUploader(
        download_fn=boom,
        upload_fn=lambda _path, timeout=120: _FakeFileRef("never"),
    )

    with pytest.raises(FileNotFoundError, match="gcs object missing"):
        uploader.normalize("clips/missing.mp4")


def test_normalize_propagates_upload_errors() -> None:
    rec_dl: list[str] = []

    def fake_dl(object_path: str, local_path: str) -> None:
        rec_dl.append(object_path)
        Path(local_path).write_bytes(b"\x00")

    def fake_up(_path: str, timeout: int = 120) -> _FakeFileRef:
        raise RuntimeError("gemini 503")

    uploader = FixtureUploader(download_fn=fake_dl, upload_fn=fake_up)
    with pytest.raises(RuntimeError, match="gemini 503"):
        uploader.normalize("clips/x.mp4")

    # Failed upload must NOT poison the cache — a retry should attempt again.
    def good_up(_path: str, timeout: int = 120) -> _FakeFileRef:
        return _FakeFileRef("files/retry-ok")

    uploader._upload = good_up  # type: ignore[attr-defined]
    assert uploader.normalize("clips/x.mp4") == f"{_GEMINI_FILE_API_BASE}/files/retry-ok"


def test_normalize_cleans_up_temp_file_on_success(tmp_path: Path) -> None:
    """Temp file used for the GCS download is removed after a successful upload."""
    captured: list[str] = []

    def fake_dl(_object_path: str, local_path: str) -> None:
        captured.append(local_path)
        Path(local_path).write_bytes(b"x")

    def fake_up(_path: str, timeout: int = 120) -> _FakeFileRef:
        return _FakeFileRef("files/ok")

    uploader = FixtureUploader(download_fn=fake_dl, upload_fn=fake_up)
    uploader.normalize("clips/x.mp4")

    assert captured, "download was never called"
    assert not Path(captured[0]).exists(), "temp file should be cleaned up"


def test_normalize_cleans_up_temp_file_on_upload_failure() -> None:
    captured: list[str] = []

    def fake_dl(_object_path: str, local_path: str) -> None:
        captured.append(local_path)
        Path(local_path).write_bytes(b"x")

    def fake_up(_path: str, timeout: int = 120) -> _FakeFileRef:
        raise RuntimeError("oops")

    uploader = FixtureUploader(download_fn=fake_dl, upload_fn=fake_up)
    with pytest.raises(RuntimeError):
        uploader.normalize("clips/x.mp4")

    assert captured
    assert not Path(captured[0]).exists(), "temp file should be cleaned up on upload failure"


# ── normalize_input ──────────────────────────────────────────────────────────


def test_normalize_input_replaces_bucket_relative_file_uri() -> None:
    rec = _Recorder(upload_id="files/r1")
    uploader = _make_uploader(rec)

    out = uploader.normalize_input(
        {"file_uri": "clips/x.mp4", "file_mime": "video/mp4", "extra": 7}
    )

    assert out["file_uri"] == f"{_GEMINI_FILE_API_BASE}/files/r1"
    assert out["file_mime"] == "video/mp4"
    assert out["extra"] == 7


def test_normalize_input_is_pure_when_already_gemini_id() -> None:
    rec = _Recorder()
    uploader = _make_uploader(rec)

    in_dict = {"file_uri": "files/already-ok", "file_mime": "video/mp4"}
    out = uploader.normalize_input(in_dict)

    # Original dict identity preserved when nothing changes — small but real
    # contract: callers can compare identity to know whether the input mutated.
    assert out is in_dict
    assert rec.uploaded == []


def test_normalize_input_noop_without_file_uri() -> None:
    rec = _Recorder()
    uploader = _make_uploader(rec)

    in_dict = {"transcript": "hi"}
    out = uploader.normalize_input(in_dict)

    assert out is in_dict
    assert rec.uploaded == []


def test_normalize_input_noop_for_non_string_file_uri() -> None:
    """Defensive: malformed fixtures with non-string file_uri shouldn't crash here."""
    rec = _Recorder()
    uploader = _make_uploader(rec)

    in_dict: dict = {"file_uri": None, "file_mime": "video/mp4"}
    out = uploader.normalize_input(in_dict)

    assert out is in_dict
    assert rec.uploaded == []


def test_normalize_input_does_not_mutate_caller_dict() -> None:
    """The returned dict is a fresh copy when the URI is rewritten."""
    rec = _Recorder(upload_id="files/r2")
    uploader = _make_uploader(rec)

    original = {"file_uri": "clips/x.mp4", "file_mime": "video/mp4"}
    snapshot = dict(original)
    out = uploader.normalize_input(original)

    assert original == snapshot, "caller's dict was mutated in place"
    assert out is not original
    assert out["file_uri"] == f"{_GEMINI_FILE_API_BASE}/files/r2"


# ── build_default_uploader (smoke) ────────────────────────────────────────────


def test_build_default_uploader_is_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the module mustn't drag in google.cloud at collection time.

    The helper does the imports inside the function body; this test just
    confirms the function is callable and returns a `FixtureUploader`. The
    GCS / Gemini code paths are exercised by integration tests, not here.
    """
    # If google.cloud isn't installed (e.g. minimal CI), skip rather than fail.
    pytest.importorskip("google.cloud.storage")
    pytest.importorskip("google.genai")
    # Avoid actually constructing the genai client.
    import app.pipeline.agents.gemini_analyzer as ga

    monkeypatch.setattr(ga, "_get_client", lambda: SimpleNamespace())
    from ._fixture_uploader import build_default_uploader

    uploader = build_default_uploader()
    assert isinstance(uploader, FixtureUploader)
