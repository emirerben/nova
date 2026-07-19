import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.pipeline.canvas import LANDSCAPE, PORTRAIT
from app.pipeline.reframe import _build_video_filter, _encoding_args, reframe_and_export


def test_encoding_args_default_matches_explicit_portrait() -> None:
    assert _encoding_args("/tmp/out.mp4") == _encoding_args("/tmp/out.mp4", canvas=PORTRAIT)


def test_encoding_args_landscape_sets_output_size() -> None:
    args = _encoding_args("/tmp/out.mp4", canvas=LANDSCAPE)

    assert args[args.index("-s") + 1] == "1920x1080"


def test_build_video_filter_default_matches_explicit_portrait() -> None:
    assert _build_video_filter("16:9", None) == _build_video_filter(
        "16:9",
        None,
        canvas=PORTRAIT,
    )


def test_build_video_filter_landscape_threads_canvas_dimensions() -> None:
    filters = _build_video_filter("16:9", None, canvas=LANDSCAPE)

    assert "scale=-2:1080" in filters
    assert "crop=1920:1080" in filters


def test_build_video_filter_landscape_letterbox_threads_canvas_dimensions() -> None:
    filters = _build_video_filter("9:16", None, output_fit="letterbox_black", canvas=LANDSCAPE)

    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in filters
    assert "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black" in filters


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)
def test_landscape_output_from_portrait_source_fills_the_real_frame(tmp_path: Path) -> None:
    """Regression: landscape must transform footage, not just widen its container."""
    source = tmp_path / "portrait-with-square.mp4"
    output = tmp_path / "landscape.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=360x640:d=0.4:r=30,drawbox=x=130:y=270:w=100:h=100:color=lime:t=fill",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )

    reframe_and_export(
        input_path=str(source),
        start_s=0.0,
        end_s=0.4,
        aspect_ratio="9:16",
        ass_subtitle_path=None,
        output_path=str(output),
        color_trc="bt709",
        has_audio=False,
        canvas=LANDSCAPE,
    )

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (1920, 1080)

    # Average a small top-left block. A contain/pad implementation would leave
    # this edge black; the production center-crop must carry source pixels all
    # the way to the frame boundary.
    edge_rgb = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(output),
            "-vf",
            "crop=16:16:0:0,scale=1:1,format=rgb24",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout[:3]
    red, green, blue = edge_rgb
    assert red > 180
    assert green < 80
    assert blue < 80

    def _strip_rgb(crop: str) -> bytes:
        return subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(output),
                "-vf",
                f"crop={crop},format=rgb24",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout

    def _lime_pixel_count(rgb: bytes) -> int:
        pixels = zip(rgb[0::3], rgb[1::3], rgb[2::3], strict=True)
        return sum(green > 120 and red < 120 and blue < 120 for red, green, blue in pixels)

    # The source marker is a square. A proportional center crop keeps it square;
    # stretching the portrait input to 16:9 would flatten it into a short, wide
    # rectangle while still satisfying the edge-fill assertion above.
    # Crop dimensions stay even for the yuv420p source; divide the duplicated
    # two-row/two-column samples back to one-dimensional lengths.
    marker_width = _lime_pixel_count(_strip_rgb("1920:2:0:540")) // 2
    marker_height = _lime_pixel_count(_strip_rgb("2:1080:960:0")) // 2
    assert marker_width > 450
    assert marker_height > 450
    assert abs(marker_width - marker_height) < 30
