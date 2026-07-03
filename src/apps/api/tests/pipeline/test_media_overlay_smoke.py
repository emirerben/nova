"""Plan 009 E7: real-ffmpeg smoke test for the fullscreen cover-crop branch.

The command-string unit tests can pass on substrings while the COMPOSED
filtergraph fails or misframes at runtime — this test actually executes ffmpeg
on synthetic inputs and probes the output:

- output stays exactly 1080x1920;
- during a fullscreen image card's window the frame is the CARD's color
  (landscape source → cover-crop fills the frame, no letterbox bars);
- outside every window the frame is the BASE color;
- a trimmed fullscreen VIDEO card composites during its window too.

Skips cleanly when ffmpeg/ffprobe are unavailable (CI images have them; the
prod Docker image ships its own).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from app.agents._schemas.media_overlay import MediaOverlay
from app.pipeline.media_overlay import build_media_overlay_command

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _gen(cmd: list[str]) -> None:
    res = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    assert res.returncode == 0, res.stderr.decode(errors="replace")[-500:]


def _make_base(path: str) -> None:
    # 6s of solid BLUE portrait canvas.
    _gen(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=1080x1920:d=6:r=24",
            "-pix_fmt",
            "yuv420p",
            path,
        ]
    )


def _make_card_image(path: str) -> None:
    # LANDSCAPE red image — cover-crop must still fill the portrait frame.
    _gen(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=640x360:d=0.1:r=24",
            "-frames:v",
            "1",
            path,
        ]
    )


def _make_card_video(path: str) -> None:
    # 4s landscape GREEN clip.
    _gen(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=320x240:d=4:r=24",
            "-pix_fmt",
            "yuv420p",
            path,
        ]
    )


def _probe_dims(path: str) -> tuple[int, int]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
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
    stream = json.loads(out.stdout)["streams"][0]
    return stream["width"], stream["height"]


def _mean_rgb_at(path: str, t: float, tmpdir: str) -> tuple[float, float, float]:
    """Extract the frame at t, downscale to 1x1, read its RGB."""
    frame = os.path.join(tmpdir, f"f_{t:.2f}.png".replace(".", "_", 1))
    _gen(
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
            "scale=1:1",
            frame,
        ]
    )
    from PIL import Image  # noqa: PLC0415 — Pillow is an api dependency

    px = Image.open(frame).convert("RGB").getpixel((0, 0))
    return float(px[0]), float(px[1]), float(px[2])


@pytest.fixture(scope="module")
def composited(tmp_path_factory) -> str:
    tmpdir = str(tmp_path_factory.mktemp("mo_smoke"))
    base = os.path.join(tmpdir, "base.mp4")
    img = os.path.join(tmpdir, "card.png")
    vid = os.path.join(tmpdir, "card.mp4")
    out = os.path.join(tmpdir, "out.mp4")
    _make_base(base)
    _make_card_image(img)
    _make_card_video(vid)

    cards = [
        MediaOverlay.model_validate(
            {
                "id": "fsimg",
                "kind": "image",
                "src_gcs_path": "users/u/plan/p/overlays/card.png",
                "display_mode": "fullscreen",
                "start_s": 1.0,
                "end_s": 2.5,
            }
        ),
        MediaOverlay.model_validate(
            {
                "id": "fsvid",
                "kind": "video",
                "src_gcs_path": "users/u/plan/p/overlays/card.mp4",
                "display_mode": "fullscreen",
                "start_s": 3.5,
                "end_s": 5.0,
                "clip_trim_start_s": 1.0,
                "clip_trim_end_s": 2.5,
                "clip_duration_s": 4.0,
                "z": 1,
            }
        ),
    ]
    cmd = build_media_overlay_command(
        base, cards, [img, vid], [c.card_width_px() for c in cards], out
    )
    _gen(cmd)
    return out


class TestFullscreenSmoke:
    def test_output_dims_unchanged(self, composited):
        assert _probe_dims(composited) == (1080, 1920)

    def test_base_visible_outside_windows(self, composited, tmp_path):
        r, g, b = _mean_rgb_at(composited, 0.4, str(tmp_path))
        assert b > r and b > g, f"expected blue base at t=0.4, got rgb=({r},{g},{b})"

    def test_image_card_covers_frame_in_window(self, composited, tmp_path):
        # Landscape red source cover-crops the full portrait frame — a 1x1
        # downscale of the frame is red-dominant ONLY if no letterbox bars.
        r, g, b = _mean_rgb_at(composited, 1.7, str(tmp_path))
        assert r > b and r > g, f"expected red takeover at t=1.7, got rgb=({r},{g},{b})"

    def test_video_card_covers_frame_in_window(self, composited, tmp_path):
        r, g, b = _mean_rgb_at(composited, 4.0, str(tmp_path))
        assert g > r and g > b, f"expected green takeover at t=4.0, got rgb=({r},{g},{b})"

    def test_base_returns_after_last_window(self, composited, tmp_path):
        r, g, b = _mean_rgb_at(composited, 5.6, str(tmp_path))
        assert b > r and b > g, f"expected blue base at t=5.6, got rgb=({r},{g},{b})"
