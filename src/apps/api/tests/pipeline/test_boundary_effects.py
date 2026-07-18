from __future__ import annotations

from app.pipeline.boundary_effects import build_boundary_effects_command


def test_horizontal_motion_blur_uses_target_pixels_and_final_encoder_policy(tmp_path) -> None:
    cmd = build_boundary_effects_command(
        "target.mp4",
        [
            {
                "effect": "horizontal_motion_blur",
                "at_s": 5.8,
                "duration_s": 0.42,
                "blur_sigma": 44.0,
                "intensity": 1.0,
            }
        ],
        str(tmp_path / "out.mp4"),
    )
    joined = " ".join(cmd)
    assert cmd.count("-i") == 1
    assert "gblur=sigma=44.000:sigmaV=1" in joined
    assert "between(T\\,5.800\\,6.220)" in joined
    assert cmd[cmd.index("-preset") + 1] == "fast"
    assert "ultrafast" not in cmd


def test_multiple_windows_compile_into_one_blend_pass(tmp_path) -> None:
    cmd = build_boundary_effects_command(
        "target.mp4",
        [
            {"effect": "horizontal_motion_blur", "at_s": 5.8, "duration_s": 0.4},
            {"effect": "horizontal_motion_blur", "at_s": 16.1, "duration_s": 0.3},
        ],
        str(tmp_path / "out.mp4"),
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "max(" in graph
    assert graph.count("split=2") == 1
