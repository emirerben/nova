"""Authored animation contracts, including execution through the real FFmpeg parser."""

import shutil
import subprocess

import pytest
from pydantic import ValidationError

from app.agents._schemas.visual_editor import VisualAnimation, VisualEditorStyle
from app.pipeline.visual_editor import sample_visual, visual_animation_filters


@pytest.mark.parametrize(
    "field,value", [("zoom", float("nan")), ("rotation_deg", 361), ("version", 2)]
)
def test_rejects_invalid_style(field, value):
    with pytest.raises(ValidationError):
        VisualEditorStyle.model_validate({field: value})


def test_speed_changes_phase_not_interval():
    slow = VisualEditorStyle(animation=VisualAnimation(entrance="pop"))
    fast = VisualEditorStyle(animation=VisualAnimation(entrance="pop", speed=2))
    assert sample_visual(slow, 0.1, 3) == sample_visual(fast, 0.05, 3)
    assert sample_visual(slow, 2.99, 3).alpha == 1
    assert sample_visual(fast, 2.99, 3).alpha == 1
    assert sample_visual(fast, 3, 3).alpha == 0


def test_independent_phases_and_loop_reference_sample():
    style = VisualEditorStyle(animation=VisualAnimation(entrance="pop", exit="slide", loop="float"))
    start = sample_visual(style, 0, 3)
    assert start.alpha == 0 and start.scale == pytest.approx(0.7)
    middle = sample_visual(style, 0.3, 3)
    assert middle.alpha == pytest.approx(0.984375)
    assert middle.scale == pytest.approx(0.9953125)
    assert middle.y == pytest.approx(-8)
    end = sample_visual(style, 2.9, 3)
    assert end.x > 0 and end.scale == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg unavailable")
@pytest.mark.parametrize("edge", ["none", "fade", "pop", "slide", "zoom"])
def test_real_ffmpeg_accepts_rotation_and_animated_alpha(edge):
    style = VisualEditorStyle(
        rotation_deg=25, animation=VisualAnimation(entrance=edge, exit=edge, loop="pulse")
    )
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=120x160:r=30:d=1",
            "-vf",
            ",".join(visual_animation_filters(style, 1)),
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
