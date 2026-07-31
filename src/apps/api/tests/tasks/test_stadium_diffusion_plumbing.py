"""End-to-end assembly plumbing for the Stadium Diffusion source look."""

from unittest.mock import MagicMock, patch

from app.pipeline.probe import VideoProbe
from app.tasks.template_orchestrate import _assemble_clips


def _look_step_and_probe(tmp_path):
    clip_file = tmp_path / "clip_look.mp4"
    clip_file.write_bytes(b"fake")
    probe = VideoProbe(
        duration_s=30.0,
        fps=30.0,
        width=1920,
        height=1080,
        has_audio=True,
        codec="h264",
        aspect_ratio="16:9",
        file_size_bytes=4,
    )
    step = MagicMock()
    step.clip_id = "clip_look"
    step.moment = {"start_s": 0.0, "end_s": 5.0}
    step.slot = {
        "position": 1,
        "target_duration_s": 5.0,
        "transition_in": "none",
        "look_preset": "stadium_diffusion",
    }
    return step, probe, clip_file


def test_source_look_reaches_multi_pass_reframe(tmp_path):
    """AssemblyStep → SlotPlan → reframe_and_export preserves the look."""
    step, probe, clip_file = _look_step_and_probe(tmp_path)
    with (
        patch("app.pipeline.reframe.reframe_and_export") as mock_reframe,
        patch("app.tasks.template_orchestrate.shutil.copy2"),
    ):
        _assemble_clips(
            steps=[step],
            clip_id_to_local={step.clip_id: str(clip_file)},
            clip_probe_map={str(clip_file): probe},
            output_path=str(tmp_path / "out.mp4"),
            tmpdir=str(tmp_path),
        )

    mock_reframe.assert_called_once()
    assert mock_reframe.call_args.kwargs["look_preset"] == "stadium_diffusion"


def test_source_look_reaches_single_pass_spec(tmp_path):
    """AssemblyStep → SlotPlan → SinglePassInput preserves the look."""
    step, probe, clip_file = _look_step_and_probe(tmp_path)
    with patch("app.tasks.template_orchestrate.run_single_pass") as mock_single:
        _assemble_clips(
            steps=[step],
            clip_id_to_local={step.clip_id: str(clip_file)},
            clip_probe_map={str(clip_file): probe},
            output_path=str(tmp_path / "out.mp4"),
            tmpdir=str(tmp_path),
            force_single_pass=True,
        )

    mock_single.assert_called_once()
    assert mock_single.call_args.args[0].inputs[0].look_preset == "stadium_diffusion"
