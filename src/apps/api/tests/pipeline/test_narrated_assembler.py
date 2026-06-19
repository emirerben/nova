from __future__ import annotations

import subprocess

from app.pipeline.narrated_alignment import StepTiming
from app.pipeline.narrated_assembler import NarratedClip, assemble_narrated


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, check=False, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def _duration(path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(path),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return float(result.stdout.decode().strip())


def _make_clip(path, color: str) -> None:
    _run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x568:d=1.2:r=30",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(path),
        ]
    )


def _make_voiceover(path) -> None:
    _run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2.0",
            "-c:a",
            "aac",
            "-y",
            str(path),
        ]
    )


def test_assemble_narrated_ffmpeg_smoke(tmp_path) -> None:
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    voiceover = tmp_path / "voiceover.m4a"
    output = tmp_path / "out.mp4"
    _make_clip(clip_a, "red")
    _make_clip(clip_b, "blue")
    _make_voiceover(voiceover)

    assemble_narrated(
        [
            StepTiming(step_id="s1", start_s=0.0, end_s=1.0, confidence=1.0),
            StepTiming(step_id="s2", start_s=1.0, end_s=2.0, confidence=1.0),
        ],
        [
            NarratedClip(step_id="s1", clip_path=str(clip_a)),
            NarratedClip(step_id="s2", clip_path=str(clip_b)),
        ],
        str(voiceover),
        str(output),
        str(tmp_path),
    )

    assert output.exists()
    assert output.stat().st_size > 0
    assert abs(_duration(output) - 2.0) <= 0.3
