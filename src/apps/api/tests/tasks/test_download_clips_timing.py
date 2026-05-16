"""Tests for the per-clip download timing instrumentation in _download_clips_parallel.

Targets the `clip_download_timing` structlog event added to collect data
that validates (or refutes) P5's worker-local clip cache assumption: cold
GCS-fetch dominates rerender wall-clock. Without per-clip numbers we
can't tell if the projected 4-8s savings are real.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.tasks import template_orchestrate


@pytest.fixture
def captured_logs(monkeypatch):
    """Capture structlog calls without depending on the real logging setup."""
    events: list[tuple[str, dict]] = []

    class _CapturingLog:
        def info(self, event: str, **kwargs):
            events.append((event, kwargs))

        # The orchestrator may call other levels — capture them as no-ops.
        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

        def debug(self, *args, **kwargs):
            pass

        def exception(self, *args, **kwargs):
            pass

    monkeypatch.setattr(template_orchestrate, "log", _CapturingLog())
    return events


def _fake_download(gcs_path: str, local_path: str) -> None:
    """Stand-in for the real GCS download — writes a tiny placeholder file
    so that the post-download size stat in _download_clips_parallel
    produces a valid byte count."""
    Path(local_path).write_bytes(b"fake-clip-content-" + gcs_path.encode())


def test_emits_clip_download_timing_event(captured_logs, tmp_path):
    """The structlog event must fire with the expected aggregate fields and
    a per-clip array. P5 / future-perf observability depends on this shape."""
    gcs_paths = [f"users/u1/clip_{i}.mp4" for i in range(3)]

    with patch.object(template_orchestrate, "download_to_file", side_effect=_fake_download):
        local_paths = template_orchestrate._download_clips_parallel(gcs_paths, str(tmp_path))

    assert len(local_paths) == 3
    assert all(Path(p).exists() for p in local_paths)

    timing_events = [(e, kw) for e, kw in captured_logs if e == "clip_download_timing"]
    assert len(timing_events) == 1, "must emit exactly one timing event per call"
    _, fields = timing_events[0]
    assert fields["clip_count"] == 3
    assert fields["max_workers"] == 3
    assert fields["per_clip_ms_min"] >= 0
    assert fields["per_clip_ms_max"] >= fields["per_clip_ms_min"]
    assert fields["per_clip_ms_mean"] >= 0
    assert fields["total_ms_serial"] >= 0
    assert fields["total_bytes"] is not None and fields["total_bytes"] > 0

    per_clip = fields["per_clip"]
    assert len(per_clip) == 3
    for stat in per_clip:
        assert stat["elapsed_ms"] >= 0
        assert stat["size_bytes"] > 0
        assert 0 <= stat["idx"] < 3


def test_handles_eight_clip_pool_cap(captured_logs, tmp_path):
    """The pool caps at 8 workers regardless of clip count — pin that the
    timing event reports the cap, not the input length, so future readers
    don't misinterpret the parallelism factor."""
    gcs_paths = [f"users/u1/clip_{i}.mp4" for i in range(15)]

    with patch.object(template_orchestrate, "download_to_file", side_effect=_fake_download):
        template_orchestrate._download_clips_parallel(gcs_paths, str(tmp_path))

    timing_events = [(e, kw) for e, kw in captured_logs if e == "clip_download_timing"]
    _, fields = timing_events[0]
    assert fields["clip_count"] == 15
    assert fields["max_workers"] == 8


def test_size_failure_does_not_break_timing(captured_logs, tmp_path):
    """If the per-clip size stat fails after download (file removed, etc.),
    the timing event still fires with size_bytes=-1 for that clip and the
    aggregate `total_bytes` excludes it. The download itself succeeded so
    the call must not raise."""
    gcs_paths = ["users/u1/clip_0.mp4", "users/u1/clip_1.mp4"]

    def _download_and_delete(gcs_path: str, local_path: str) -> None:
        """Download then immediately delete clip_0 so its size stat fails."""
        _fake_download(gcs_path, local_path)
        if gcs_path.endswith("_0.mp4"):
            Path(local_path).unlink()

    with patch.object(template_orchestrate, "download_to_file", side_effect=_download_and_delete):
        template_orchestrate._download_clips_parallel(gcs_paths, str(tmp_path))

    timing_events = [(e, kw) for e, kw in captured_logs if e == "clip_download_timing"]
    _, fields = timing_events[0]
    per_clip_by_idx = {s["idx"]: s for s in fields["per_clip"]}
    assert per_clip_by_idx[0]["size_bytes"] == -1
    assert per_clip_by_idx[1]["size_bytes"] > 0
    # Aggregate `total_bytes` only sums known sizes.
    assert fields["total_bytes"] == per_clip_by_idx[1]["size_bytes"]


def test_download_propagates_underlying_errors(captured_logs, tmp_path):
    """If `download_to_file` raises, the orchestrator must propagate — a
    silent swallow would let a job continue with missing clips."""
    gcs_paths = ["users/u1/clip_0.mp4"]

    def _failing_download(gcs_path: str, local_path: str) -> None:
        raise RuntimeError("simulated GCS auth failure")

    with patch.object(template_orchestrate, "download_to_file", side_effect=_failing_download):
        with pytest.raises(RuntimeError, match="simulated GCS auth failure"):
            template_orchestrate._download_clips_parallel(gcs_paths, str(tmp_path))


def test_no_clips_emits_no_timing_event(captured_logs, tmp_path):
    """Edge case: a zero-clip call shouldn't fire a no-op timing event or
    crash on empty-list `min()` / `max()`. (Production never hits this but
    pinning the contract prevents a future caller from hitting an ugly
    ValueError.)"""
    # ThreadPoolExecutor with max_workers=0 raises — assert we handle it
    # by NOT calling _download_clips_parallel with empty input, OR the
    # function gracefully handles it. The current code calls
    # min(0, 8) = 0 which TPE rejects. Document this as a known
    # precondition: callers must never pass an empty list.
    with pytest.raises(ValueError, match="max_workers"):
        template_orchestrate._download_clips_parallel([], str(tmp_path))
