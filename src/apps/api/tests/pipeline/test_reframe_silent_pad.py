"""The silent audio pad on a sound-less reframe ends where the picture is planned to.

Sources without sound get a lavfi ``anullsrc`` track so every rendered clip
keeps the H.264/AAC contract. ``anullsrc`` never ends on its own, and
``-shortest`` cannot be trusted to stop it: FFmpeg before 8.1 (CI 8.0.1, prod
7.1.5) lacks the scheduler choke fixes (FFmpeg 9d0b88feb, 5f4cbb561), so under
CPU load the silence races ahead of a slow video and ``-shortest``'s sync-queue
overflow releases audio past the last frame. Guided-story moments then probe
0.26–0.39 s long and fail their receipt (``guided_story_receipt_mismatch``,
the intermittent ``test_guided_story_ffmpeg.py`` failures of 2026-10-27).

The worst case of that race is no bound at all, so these renders drop
``-shortest`` from the real FFmpeg command: the pad must stop on its own.

Ways this can fail:
1. The pad is unbounded (old behaviour): FFmpeg never finishes without
   ``-shortest`` and the render times out.
2. The pad is bounded by the source window instead of the output length:
   a 2x clip keeps twice its audio; a 0.5x clip loses half of it.
3. The bound lands on the wrong ``-i`` (the source, or the output), which
   trims the picture instead of the silence.
4. The overlay or keep-segments builder keeps an unbounded pad while the
   plain builder is fixed.
5. The keep-segments bound follows the planned seconds rather than the
   snapped frame grid, so the pad drifts from the concatenated picture.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.pipeline import reframe
from app.pipeline.canvas import Canvas
from app.pipeline.reframe import reframe_and_export

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)

CANVAS = Canvas(320, 568)
# One AAC frame at 44.1 kHz is 23 ms; the pad may end inside its last frame.
PAD_TOLERANCE_S = 0.05
# The guided-story moment receipt (_render_moments).
RECEIPT_TOLERANCE_S = 0.15


def _source(path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x568:rate=30",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return path


def _durations(path: Path) -> dict[str, float]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    data = json.loads(result.stdout)
    durations = {stream["codec_type"]: float(stream["duration"]) for stream in data["streams"]}
    durations["container"] = float(data["format"]["duration"])
    return durations


@pytest.fixture
def without_shortest(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    real_run = subprocess.run
    stripped: list[list[str]] = []

    def run(cmd, *args, **kwargs):
        if "-shortest" in cmd:
            cmd = [arg for arg in cmd if arg != "-shortest"]
            stripped.append(cmd)
            # An unbounded pad never finishes: fail inside pytest's 30 s limit.
            kwargs["timeout"] = 20
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(reframe.subprocess, "run", run)
    return stripped


def _overlay(tmp_path: Path) -> list[dict]:
    png = tmp_path / "overlay.png"
    Image.new("RGBA", (CANVAS.width, CANVAS.height), (255, 255, 255, 96)).save(png)
    return [{"png_path": str(png), "start_s": 0.0, "end_s": 0.5}]


@pytest.mark.parametrize(
    ("case", "window_s", "speed_factor", "planned_s"),
    [
        ("plain", 1.0, 1.0, 1.0),
        ("plain", 2.0, 2.0, 1.0),
        ("plain", 1.0, 0.5, 2.0),
        ("overlay", 1.0, 1.0, 1.0),
        ("keep_segments", 2.0, 1.0, 1.0),
    ],
)
def test_silent_pad_stops_at_the_planned_length_on_its_own(
    tmp_path: Path,
    without_shortest: list[list[str]],
    case: str,
    window_s: float,
    speed_factor: float,
    planned_s: float,
) -> None:
    source = _source(tmp_path / "source.mp4")
    output = tmp_path / "out.mp4"
    extra: dict = {}
    if case == "overlay":
        extra["text_overlay_pngs"] = _overlay(tmp_path)
    if case == "keep_segments":
        extra["keep_segments"] = [(0.0, 0.5), (1.0, 1.5)]

    reframe_and_export(
        input_path=str(source),
        start_s=0.5,
        end_s=0.5 + window_s,
        aspect_ratio="9:16",
        ass_subtitle_path=None,
        output_path=str(output),
        color_trc="bt709",
        has_audio=False,
        speed_factor=speed_factor,
        canvas=CANVAS,
        **extra,
    )

    assert len(without_shortest) == 1
    durations = _durations(output)
    assert durations["audio"] == pytest.approx(planned_s, abs=PAD_TOLERANCE_S)
    assert durations["video"] == pytest.approx(planned_s, abs=RECEIPT_TOLERANCE_S)
    assert durations["container"] == pytest.approx(planned_s, abs=RECEIPT_TOLERANCE_S)
