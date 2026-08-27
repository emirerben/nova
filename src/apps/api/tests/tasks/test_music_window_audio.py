"""Audio-mixer coverage for validated user-selected song windows."""

from unittest.mock import MagicMock, patch

import pytest


def test_validated_window_bypasses_legacy_five_second_clamp(tmp_path) -> None:
    """A server-validated near-EOF selection must keep its exact seek."""
    from app.tasks.template_orchestrate import _mix_template_audio

    ok_proc = MagicMock(returncode=0)
    captured: dict = {}

    def _capture(cmd, *_args, **_kwargs):
        captured["cmd"] = cmd
        return ok_proc

    with (
        patch("app.tasks.template_orchestrate.download_to_file"),
        patch(
            "app.tasks.template_orchestrate._probe_duration",
            side_effect=[1.0, 30.0],
        ),
        patch("app.tasks.template_orchestrate.subprocess.run", side_effect=_capture),
    ):
        _mix_template_audio(
            video_path="/tmp/assembled.mp4",
            audio_gcs_path="templates/t1/audio.m4a",
            output_path=str(tmp_path / "final.mp4"),
            tmpdir=str(tmp_path),
            audio_start_offset_s=29.0,
            validated_window_duration_s=1.0,
        )

    cmd = captured["cmd"]
    assert cmd[cmd.index("-ss") + 1] == "29.000"


def test_guided_audio_window_trims_source_and_pads_silence(tmp_path) -> None:
    from app.tasks.template_orchestrate import _mix_template_audio

    captured: dict = {}
    ok_proc = MagicMock(returncode=0)

    def _capture(cmd, *_args, **_kwargs):
        captured["cmd"] = cmd
        return ok_proc

    with (
        patch("app.tasks.template_orchestrate.download_to_file"),
        patch("app.tasks.template_orchestrate._probe_duration", side_effect=[10.0, 60.0]),
        patch("app.tasks.template_orchestrate.subprocess.run", side_effect=_capture),
    ):
        _mix_template_audio(
            "/tmp/assembled.mp4",
            "music/t1.m4a",
            str(tmp_path / "final.mp4"),
            str(tmp_path),
            audio_start_offset_s=12.0,
            validated_window_duration_s=3.0,
            audio_window_duration_s=3.0,
            force_video_duration=True,
        )

    cmd = captured["cmd"]
    af = cmd[cmd.index("-af") + 1]
    assert "atrim=duration=3.000000" in af
    assert "apad" in af
    assert cmd[cmd.index("-ss") + 1] == "12.000"
    assert cmd[cmd.index("-t") + 1] == "10.000"


def test_guided_audio_clamps_bounded_cfr_overrun_to_approved_duration(tmp_path) -> None:
    from app.tasks.template_orchestrate import _mix_template_audio

    captured: dict = {}
    ok_proc = MagicMock(returncode=0)

    def _capture(cmd, *_args, **_kwargs):
        captured["cmd"] = cmd
        return ok_proc

    with (
        patch("app.tasks.template_orchestrate.download_to_file"),
        patch("app.tasks.template_orchestrate._probe_duration", side_effect=[24.2, 60.0]),
        patch("app.tasks.template_orchestrate.subprocess.run", side_effect=_capture),
    ):
        _mix_template_audio(
            "/tmp/assembled.mp4",
            "music/t1.m4a",
            str(tmp_path / "final.mp4"),
            str(tmp_path),
            validated_window_duration_s=24.0,
            audio_window_duration_s=24.0,
            force_video_duration=True,
            target_video_duration_s=24.0,
        )

    cmd = captured["cmd"]
    assert cmd[cmd.index("-t") + 1] == "24.000"


def test_guided_audio_rejects_overrun_larger_than_frame_rounding(tmp_path) -> None:
    from app.tasks.template_orchestrate import _mix_template_audio

    with (
        patch("app.tasks.template_orchestrate.download_to_file"),
        patch("app.tasks.template_orchestrate._probe_duration", side_effect=[24.3, 60.0]),
    ):
        with pytest.raises(RuntimeError, match="more than frame rounding"):
            _mix_template_audio(
                "/tmp/assembled.mp4",
                "music/t1.m4a",
                str(tmp_path / "final.mp4"),
                str(tmp_path),
                force_video_duration=True,
                target_video_duration_s=24.0,
            )


def test_guided_audio_rejects_underrun_without_invoking_ffmpeg(tmp_path) -> None:
    from app.tasks.template_orchestrate import _mix_template_audio

    with (
        patch("app.tasks.template_orchestrate.download_to_file"),
        patch("app.tasks.template_orchestrate._probe_duration", side_effect=[23.7, 60.0]),
        patch("app.tasks.template_orchestrate.subprocess.run") as run,
    ):
        with pytest.raises(RuntimeError, match="shorter than its approved duration"):
            _mix_template_audio(
                "/tmp/assembled.mp4",
                "music/t1.m4a",
                str(tmp_path / "final.mp4"),
                str(tmp_path),
                force_video_duration=True,
                target_video_duration_s=24.0,
            )

    run.assert_not_called()
