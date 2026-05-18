"""Stage A of the Layer-2 text-overlay pipeline: frame extraction via ffmpeg.

`extract_frames(video_path, out_dir, fps)` samples a video at the given rate
and writes the frames to `out_dir` as JPEGs, returning a list of
`ExtractedFrame(path, t_s)` sorted by timestamp. The downstream OCR stage
calls the backend once per `ExtractedFrame.path` and stamps the result with
`ExtractedFrame.t_s`.

Sampling rate is fixed during the run (no adaptive sampling) — at 2 fps,
a 10s template produces 20 frames, a 60s template produces 120 frames.
That matches the design doc's cost budget (~$0.03 / 10s template at
Cloud Vision rates). The fps is exposed as a parameter so the eval
harness and live pipeline can choose independently.

ffmpeg is already a project dep (used by reframe, interstitials,
template_orchestrate, music recipe — see CLAUDE.md). The dev/CI image
ships with it; production Fly image inherits the same.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import structlog

log = structlog.get_logger()

DEFAULT_FPS = 2.0


@dataclass(frozen=True)
class ExtractedFrame:
    """One frame extracted from a source video.

    `path` is the absolute path to the JPEG on disk. `t_s` is the global
    timeline second of the frame (computed from the frame's position in the
    sampling sequence, not read back from ffmpeg's per-frame metadata —
    ffmpeg's `-vf fps=N` filter samples at uniform intervals 1/N apart
    starting at t=0, so position-based calculation matches the actual
    sample time).
    """

    path: str
    t_s: float


def extract_frames(
    video_path: str | Path,
    *,
    out_dir: str | Path,
    fps: float = DEFAULT_FPS,
) -> list[ExtractedFrame]:
    """Sample frames from `video_path` at `fps` and write them under `out_dir`.

    Returns frames sorted by `t_s` ascending. Filenames are zero-padded
    five-digit indices (`00001.jpg`, ...) so the on-disk order matches
    the temporal order without parsing a timestamp from the filename.

    Out-dir is created if missing. The caller owns cleanup — see
    `frames_workdir()` for an auto-cleaning tempdir.

    Raises:
        RuntimeError if ffmpeg fails or produces no frames.
    """
    video_path = str(video_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if fps <= 0.0:
        raise ValueError(f"fps must be positive, got {fps}")

    output_pattern = str(out_dir / "%05d.jpg")
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        video_path,
        "-vf",
        f"fps={fps}",
        "-qscale:v",
        "2",  # high-quality JPEG; OCR quality drops noticeably on lower settings
        output_pattern,
    ]
    log.info("video_frames_extract_start", video=video_path, fps=fps, out_dir=str(out_dir))
    result = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-1000:]
        raise RuntimeError(f"ffmpeg failed (rc={result.returncode}): {stderr}")

    # Scan out_dir for produced frames. ffmpeg writes them in order so
    # sorted(filenames) yields temporal order. Frame N (1-indexed) is at
    # t = (N - 1) / fps because the fps filter samples at uniform 1/fps
    # intervals starting from t=0.
    files = sorted(p for p in out_dir.iterdir() if p.suffix == ".jpg")
    if not files:
        raise RuntimeError(
            f"ffmpeg produced no frames for {video_path}. "
            "Check that the video is decodable and longer than 1/fps seconds."
        )

    frames = [ExtractedFrame(path=str(f), t_s=i / fps) for i, f in enumerate(files)]
    log.info(
        "video_frames_extract_done",
        video=video_path,
        fps=fps,
        n_frames=len(frames),
        out_dir=str(out_dir),
    )
    return frames


@contextmanager
def frames_workdir(out_dir: str | Path | None = None) -> Iterator[Path]:
    """Yield a directory for frame extraction.

    If `out_dir` is None, yields a tempdir that is cleaned up on context
    exit. If provided, yields the given path as-is (creating it if needed)
    and does NOT clean up — the caller stays in control. This split lets
    eval harnesses keep frames around for debugging while the production
    pipeline runs ephemerally.
    """
    if out_dir is None:
        with TemporaryDirectory(prefix="nova-frames-") as td:
            yield Path(td)
    else:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        yield out_dir


def have_ffmpeg() -> bool:
    """Whether `ffmpeg` is available on PATH. Used by tests to skip gracefully."""
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5, check=False)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# Module-level constant for callers that need the path layout.
JPEG_FILE_PATTERN = "%05d.jpg"
