"""Plan 009 E3: fullscreen pass timeout ceiling + one-shot veryfast retry.

Pins the encode-budget decision from the eng review:
- fullscreen passes run with the RAISED subprocess timeout (1200s, not 600s);
- on TimeoutExpired a fullscreen pass retries EXACTLY ONCE with
  force_veryfast=True (never fails the variant on the preset choice);
- pip-only passes keep the 600s ceiling and do NOT retry (propagate).
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.agents._schemas.media_overlay import MediaOverlay
from app.pipeline import media_overlay as mo


def _fs_video_card() -> MediaOverlay:
    return MediaOverlay.model_validate(
        {
            "id": "fs1",
            "kind": "video",
            "src_gcs_path": "users/u/plan/p/overlays/clip.mp4",
            "display_mode": "fullscreen",
            "start_s": 1.0,
            "end_s": 3.0,
            "clip_trim_start_s": 0.0,
            "clip_trim_end_s": 2.0,
            "clip_duration_s": 5.0,
        }
    )


def _pip_card() -> MediaOverlay:
    return MediaOverlay.model_validate(
        {
            "id": "pip1",
            "kind": "video",
            "src_gcs_path": "users/u/plan/p/overlays/clip.mp4",
            "start_s": 1.0,
            "end_s": 3.0,
        }
    )


def _fake_download(_gcs, local):  # noqa: ANN001
    with open(local, "wb") as f:
        f.write(b"x")


def _run_apply(card, run_side_effect):
    """Drive apply_media_overlays with storage + subprocess patched."""
    calls: list[list[str]] = []

    def _run(cmd, **_kw):  # noqa: ANN001
        calls.append(list(cmd))
        return run_side_effect(len(calls), cmd)

    with (
        patch.object(mo.storage, "download_to_file", side_effect=_fake_download),
        patch.object(mo.storage, "upload_public_read", return_value="https://signed"),
        patch.object(mo.subprocess, "run", side_effect=_run),
    ):
        url = mo.apply_media_overlays("base/key.mp4", [card], "out/key.mp4", job_id="j1")
    return url, calls


def _timeout_kwargs_of(mock_run_calls):
    return [c.kwargs.get("timeout") for c in mock_run_calls]


class TestFullscreenTimeoutRetry:
    def test_fullscreen_uses_raised_timeout(self):
        seen_timeouts: list[int] = []

        def _run(cmd, **kw):  # noqa: ANN001
            seen_timeouts.append(kw.get("timeout"))
            return MagicMock(returncode=0, stderr=b"")

        with (
            patch.object(mo.storage, "download_to_file", side_effect=_fake_download),
            patch.object(mo.storage, "upload_public_read", return_value="https://signed"),
            patch.object(mo.subprocess, "run", side_effect=_run),
        ):
            mo.apply_media_overlays("base/key.mp4", [_fs_video_card()], "out/key.mp4")
        assert seen_timeouts == [mo._TIMEOUT_FULLSCREEN_S]

    def test_timeout_retries_once_at_veryfast(self):
        def _side_effect(call_no, cmd):
            if call_no == 1:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=mo._TIMEOUT_FULLSCREEN_S)
            return MagicMock(returncode=0, stderr=b"")

        url, calls = _run_apply(_fs_video_card(), _side_effect)
        assert url == "https://signed"
        assert len(calls) == 2
        first_preset = calls[0][calls[0].index("-preset") + 1]
        second_preset = calls[1][calls[1].index("-preset") + 1]
        assert first_preset == "fast"
        assert second_preset == "veryfast"

    def test_second_timeout_propagates(self):
        def _side_effect(call_no, cmd):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=mo._TIMEOUT_FULLSCREEN_S)

        with pytest.raises(subprocess.TimeoutExpired):
            _run_apply(_fs_video_card(), _side_effect)

    def test_pip_only_timeout_does_not_retry(self):
        attempts: list[int] = []

        def _side_effect(call_no, cmd):
            attempts.append(call_no)
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=mo._TIMEOUT_PIP_S)

        with pytest.raises(subprocess.TimeoutExpired):
            _run_apply(_pip_card(), _side_effect)
        assert attempts == [1]

    def test_pip_only_keeps_600s_ceiling(self):
        seen_timeouts: list[int] = []

        def _run(cmd, **kw):  # noqa: ANN001
            seen_timeouts.append(kw.get("timeout"))
            return MagicMock(returncode=0, stderr=b"")

        with (
            patch.object(mo.storage, "download_to_file", side_effect=_fake_download),
            patch.object(mo.storage, "upload_public_read", return_value="https://signed"),
            patch.object(mo.subprocess, "run", side_effect=_run),
        ):
            mo.apply_media_overlays("base/key.mp4", [_pip_card()], "out/key.mp4")
        assert seen_timeouts == [mo._TIMEOUT_PIP_S]
