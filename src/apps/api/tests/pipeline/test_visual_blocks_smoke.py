"""Small real-FFmpeg smoke test for the first-class media-block compositor."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from app.agents._schemas.visual_block import MediaBlock, MediaTransform
from app.pipeline.visual_blocks import build_visual_block_composite_command

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")[-1000:]


def _dimensions(path: str) -> tuple[int, int]:
    result = subprocess.run(
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
            path,
        ],
        capture_output=True,
        timeout=60,
        check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    return stream["width"], stream["height"]


def _rgb_at(path: str, t: float, x: int, y: int, tmpdir: str) -> tuple[int, int, int]:
    """Extract a tiny representative patch without decoding the full frame."""
    frame = os.path.join(tmpdir, f"pixel_{t:.3f}_{x}_{y}.png")
    _run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{t:.3f}",
            "-i",
            path,
            "-frames:v",
            "1",
            "-vf",
            # Keep the crop even-sized so the yuv420p source can pass through
            # FFmpeg's crop filter before the PNG conversion.
            f"crop=2:2:{x}:{y}",
            frame,
        ]
    )
    from PIL import Image  # noqa: PLC0415 - keep the smoke helper lightweight

    return Image.open(frame).convert("RGB").getpixel((0, 0))


def _make_image(path: str, color: str) -> None:
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=640x360:d=0.1:r=24",
            "-frames:v",
            "1",
            path,
        ]
    )


def _make_split_image(path: str) -> None:
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x360:d=0.1:r=24",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=320x360:d=0.1:r=24",
            "-filter_complex",
            "[0:v][1:v]hstack=inputs=2",
            "-frames:v",
            "1",
            path,
        ]
    )


@pytest.fixture(scope="module")
def composited(tmp_path_factory) -> str:
    tmpdir = tmp_path_factory.mktemp("visual_blocks_smoke")
    base = str(tmpdir / "base.mp4")
    red = str(tmpdir / "red.png")
    green = str(tmpdir / "green.png")
    focal = str(tmpdir / "focal.png")
    video = str(tmpdir / "video.mp4")
    output = str(tmpdir / "out.mp4")
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=1080x1920:d=3:r=24",
            "-pix_fmt",
            "yuv420p",
            base,
        ]
    )
    _make_image(red, "red")
    _make_image(green, "green")
    _make_split_image(focal)
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=yellow:s=320x240:d=0.8:r=24",
            "-pix_fmt",
            "yuv420p",
            video,
        ]
    )

    cards = [
        # Landscape contain leaves the blue base visible above/below the card.
        MediaBlock(
            kind="media",
            asset_id="contain",
            src_gcs_path=red,
            media_kind="image",
            display_mode="fullscreen",
            start_s=0.2,
            end_s=0.6,
            transform=MediaTransform(fit_mode="contain"),
            z=0,
        ),
        # Same-window PiP cards prove that a higher z layer wins at overlap.
        MediaBlock(
            kind="media",
            asset_id="pip-low",
            src_gcs_path=red,
            media_kind="image",
            display_mode="overlay",
            start_s=0.7,
            end_s=1.1,
            x_frac=0.5,
            y_frac=0.5,
            scale=0.4,
            z=1,
        ),
        MediaBlock(
            kind="media",
            asset_id="pip-high",
            src_gcs_path=green,
            media_kind="image",
            display_mode="overlay",
            start_s=0.7,
            end_s=1.1,
            x_frac=0.5,
            y_frac=0.5,
            scale=0.4,
            z=2,
        ),
        # Adjacent windows are intentionally frame-aligned at 1.5s. Since
        # enable uses [start,end), the green card owns the exact cut frame.
        MediaBlock(
            kind="media",
            asset_id="cut-red",
            src_gcs_path=red,
            media_kind="image",
            display_mode="fullscreen",
            start_s=1.1,
            end_s=1.5,
            transform=MediaTransform(fit_mode="cover"),
            z=3,
        ),
        MediaBlock(
            kind="media",
            asset_id="cut-green",
            src_gcs_path=green,
            media_kind="image",
            display_mode="fullscreen",
            start_s=1.5,
            end_s=2.0,
            transform=MediaTransform(fit_mode="cover"),
            z=3,
        ),
        MediaBlock(
            kind="media",
            asset_id="trimmed-video",
            src_gcs_path=video,
            media_kind="video",
            display_mode="fullscreen",
            start_s=2.2,
            end_s=2.7,
            source_duration_s=0.8,
            trim_start_s=0.1,
            trim_end_s=0.6,
            transform=MediaTransform(fit_mode="cover"),
            z=4,
        ),
        # A split-color landscape source makes focal positioning observable:
        # right-edge cover must select the green half, not the red half.
        MediaBlock(
            kind="media",
            asset_id="cover-focal",
            src_gcs_path=focal,
            media_kind="image",
            display_mode="fullscreen",
            start_s=2.75,
            end_s=2.95,
            transform=MediaTransform(fit_mode="cover", focal_x=1.0, focal_y=0.5),
            z=5,
        ),
    ]
    _run(
        build_visual_block_composite_command(
            base, cards, [red, red, green, red, green, video, focal], output
        )
    )
    return output


def test_media_block_smoke_keeps_canvas_dimensions(composited: str) -> None:
    assert _dimensions(composited) == (1080, 1920)


def test_contain_reveals_base_around_media(composited: str, tmp_path) -> None:
    top = _rgb_at(composited, 0.4, 540, 10, str(tmp_path))
    center = _rgb_at(composited, 0.4, 540, 960, str(tmp_path))
    assert top[2] > top[0] + 40 and top[2] > top[1] + 20, top
    assert center[0] > center[1] + 40 and center[0] > center[2] + 40, center


def test_overlapping_media_honors_z_order(composited: str, tmp_path) -> None:
    center = _rgb_at(composited, 0.85, 540, 960, str(tmp_path))
    assert center[1] > center[0] + 30 and center[1] > center[2] + 20, center


def test_adjacent_media_cut_is_half_open(composited: str, tmp_path) -> None:
    before = _rgb_at(composited, 1.4, 540, 960, str(tmp_path))
    at_boundary = _rgb_at(composited, 1.5, 540, 960, str(tmp_path))
    assert before[0] > before[1] + 40 and before[0] > before[2] + 40, before
    assert at_boundary[1] > at_boundary[0] + 40 and at_boundary[1] > at_boundary[2] + 20, (
        at_boundary
    )


def test_trimmed_video_media_composites(composited: str, tmp_path) -> None:
    center = _rgb_at(composited, 2.5, 540, 960, str(tmp_path))
    assert center[0] > 150 and center[1] > 150 and center[2] < 80, center


def test_cover_media_honors_focal_point_in_rendered_pixels(composited: str, tmp_path) -> None:
    center = _rgb_at(composited, 2.85, 540, 960, str(tmp_path))
    assert center[1] > center[0] + 30 and center[1] > center[2] + 20, center
