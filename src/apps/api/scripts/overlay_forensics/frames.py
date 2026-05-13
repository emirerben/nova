"""FFmpeg-based frame sampling and audio extraction.

Uses subprocess FFmpeg (not MoviePy — see CLAUDE.md). Frames are decoded
into a temp directory as PNG and loaded one at a time as numpy arrays so
RAM stays flat regardless of video length.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import numpy as np
from PIL import Image


class FFmpegNotInstalledError(RuntimeError):
    """ffmpeg / ffprobe binary missing on PATH."""


class VideoReadError(RuntimeError):
    """ffmpeg returned nonzero while decoding the video."""


def check_ffmpeg_installed() -> None:
    """Raise FFmpegNotInstalledError if ffmpeg or ffprobe is missing on PATH."""
    if shutil.which("ffmpeg") is None:
        raise FFmpegNotInstalledError("ffmpeg not found on PATH")
    if shutil.which("ffprobe") is None:
        raise FFmpegNotInstalledError("ffprobe not found on PATH")


def probe_video(path: Path) -> dict:
    """Return width / height / fps / duration_s for the first video stream.

    Raises VideoReadError on ffprobe failure.
    """
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,duration",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=0",
            str(path),
        ],
        check=False, capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise VideoReadError(f"ffprobe failed: {out.stderr.strip()}")

    info: dict[str, str] = {}
    for line in out.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()

    fps_str = info.get("r_frame_rate", "30/1")
    try:
        num, den = fps_str.split("/", 1)
        fps = float(num) / float(den) if float(den) != 0 else 30.0
    except (ValueError, ZeroDivisionError):
        fps = 30.0

    # Stream duration is sometimes "N/A"; format duration is the fallback.
    duration_raw = info.get("duration", "")
    if not duration_raw or duration_raw == "N/A":
        # The second show_entries call writes a second duration= line.
        # The fallback above already picks the last one, which is format-level.
        duration = 0.0
    else:
        try:
            duration = float(duration_raw)
        except ValueError:
            duration = 0.0

    return {
        "width": int(info.get("width", 0)),
        "height": int(info.get("height", 0)),
        "fps": fps,
        "duration_s": duration,
    }


def sample_frames(
    video_path: Path,
    t_start: float,
    t_end: float,
    step_s: float,
) -> Iterator[tuple[float, np.ndarray]]:
    """Yield (timestamp_s, frame_rgb_array) for each sampled frame in [t_start, t_end).

    Frames are decoded into a tempdir, yielded one at a time, then the dir is
    cleaned up. Sample rate = 1 / step_s. Each frame array shape is (H, W, 3).
    """
    if step_s <= 0:
        raise ValueError(f"step_s must be > 0, got {step_s}")
    if t_end <= t_start:
        raise ValueError(f"t_end ({t_end}) must be > t_start ({t_start})")

    duration_window = t_end - t_start
    fps_sample = 1.0 / step_s

    with tempfile.TemporaryDirectory(prefix="ovf_frames_") as td:
        td_path = Path(td)
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-ss", f"{t_start:.4f}",
            "-i", str(video_path),
            "-t", f"{duration_window:.4f}",
            "-vf", f"fps={fps_sample}",
            "-y",
            str(td_path / "f_%05d.png"),
        ]
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise VideoReadError(
                f"ffmpeg frame extraction failed: {result.stderr.strip()}"
            )

        frame_paths = sorted(td_path.glob("f_*.png"))
        if not frame_paths:
            raise VideoReadError(
                f"ffmpeg produced no frames for window [{t_start}, {t_end}) of {video_path}"
            )

        for i, fp in enumerate(frame_paths):
            ts = t_start + i * step_s
            arr = np.array(Image.open(fp).convert("RGB"))
            yield ts, arr


def extract_audio(video_path: Path, out_path: Path) -> bool:
    """Extract the first audio stream to out_path (any container ffmpeg infers).

    Returns True on success, False if the video has no audio stream. Other
    ffmpeg errors raise VideoReadError.
    """
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-i", str(video_path),
        "-vn", "-acodec", "copy",
        "-y", str(out_path),
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode == 0:
        return True
    stderr = (result.stderr or "").lower()
    no_stream_markers = (
        "does not contain any stream",
        "output file does not contain any stream",
    )
    if any(m in stderr for m in no_stream_markers):
        return False
    # Some videos store audio in a codec that can't be `copy`'d into the
    # inferred container — fall back to re-encoding to AAC.
    cmd_reencode = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-i", str(video_path),
        "-vn", "-c:a", "aac", "-b:a", "128k",
        "-y", str(out_path),
    ]
    result2 = subprocess.run(cmd_reencode, check=False, capture_output=True, text=True)
    if result2.returncode == 0:
        return True
    stderr2 = (result2.stderr or "").lower()
    if "does not contain any stream" in stderr2 or "no audio" in stderr2:
        return False
    raise VideoReadError(
        f"ffmpeg audio extraction failed: {result2.stderr.strip() or result.stderr.strip()}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m scripts.overlay_forensics.frames <video.mp4>", file=sys.stderr)
        sys.exit(2)
    check_ffmpeg_installed()
    print(probe_video(Path(sys.argv[1])))
