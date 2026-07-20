"""Audio-mixer coverage for validated user-selected song windows."""

from unittest.mock import MagicMock, patch


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
