"""Strict output-duration coverage for guided video moments."""

import json
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

from app.pipeline.canvas import Canvas
from app.pipeline.guided_story import _enforce_strict_story_duration
from app.pipeline.probe import probe_video
from app.pipeline.reframe import reframe_and_export

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


def test_exact_duration_adds_output_cap_after_cfr(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0, stderr=b"")

    monkeypatch.setattr("app.pipeline.reframe.subprocess.run", fake_run)
    monkeypatch.setattr("app.pipeline.reframe.os.path.exists", lambda _path: True)
    monkeypatch.setattr("app.pipeline.reframe.os.path.getsize", lambda _path: 1024)

    reframe_and_export(
        input_path="/fake/in.mp4",
        start_s=1.0,
        end_s=3.0,
        aspect_ratio="16:9",
        ass_subtitle_path=None,
        output_path=str(tmp_path / "out.mp4"),
        color_trc="bt709",
        has_audio=False,
        exact_duration=True,
    )

    command = calls[0]
    duration_options = [index for index, value in enumerate(command) if value == "-t"]
    assert len(duration_options) == 2
    assert duration_options[-1] > command.index("-i")
    assert command[duration_options[-1] + 1] == "2.000000"


def test_exact_duration_rejects_unsupported_overlay_path(tmp_path) -> None:
    with pytest.raises(ValueError, match="overlay-free"):
        reframe_and_export(
            input_path="/fake/in.mp4",
            start_s=1.0,
            end_s=3.0,
            aspect_ratio="16:9",
            ass_subtitle_path=None,
            output_path=str(tmp_path / "out.mp4"),
            text_overlay_pngs=[{"png_path": "/fake/overlay.png", "start_s": 0, "end_s": 1}],
            color_trc="bt709",
            has_audio=False,
            exact_duration=True,
        )


@needs_ffmpeg
def test_exact_duration_caps_2997_source_to_sixty_frames(tmp_path) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30000/1001",
            "-t",
            "5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )

    reframe_and_export(
        input_path=str(source),
        start_s=0.0,
        end_s=2.0,
        aspect_ratio="16:9",
        ass_subtitle_path=None,
        output_path=str(output),
        color_trc="bt709",
        has_audio=False,
        canvas=Canvas(width=320, height=568),
        exact_duration=True,
    )

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=duration,nb_frames,width,height",
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
    assert stream == {
        "width": 320,
        "height": 568,
        "duration": "2.000000",
        "nb_frames": "60",
    }


@needs_ffmpeg
def test_strict_story_cap_preserves_h264_aac_and_target_duration(tmp_path) -> None:
    source = tmp_path / "source-with-audio.mp4"
    output = tmp_path / "capped-with-audio.mp4"
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
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "2.2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )

    capped = _enforce_strict_story_duration(str(source), str(output), target_s=2.0)

    assert capped == str(output)
    video = probe_video(capped)
    assert (video.codec, video.has_audio, video.width, video.height) == (
        "h264",
        True,
        320,
        568,
    )
    assert video.duration_s == pytest.approx(2.0, abs=0.15)
    audio_probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,duration",
            "-of",
            "json",
            capped,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    audio = json.loads(audio_probe.stdout)["streams"][0]
    assert audio["codec_name"] == "aac"
    assert float(audio["duration"]) == pytest.approx(2.0, abs=0.15)
