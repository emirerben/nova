"""Card media resolution: extracting a still frame from a source clip to use as a
carousel card's image."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

FRAME_EXTRACT_TIMEOUT_S = 30


@dataclass(frozen=True)
class CardAsset:
    index: int
    image_path: str  # local PNG extracted from a source clip


def resolve_card_media(
    clip_path: str, work_dir: str, index: int, at_frac: float = 0.4
) -> CardAsset:
    """Extract one still frame from `clip_path` at `duration * at_frac` and write it
    to `{work_dir}/card_{index:02d}.png`.

    Raises RuntimeError (never a pipeline-specific exception) on any ffprobe/ffmpeg
    failure — `segment.py`'s never-raise wrapper is the catch point for callers.
    """
    from app.pipeline.probe import ProbeError, probe_video  # noqa: PLC0415

    try:
        video_probe = probe_video(clip_path)
    except ProbeError as exc:
        raise RuntimeError(
            f"resolve_card_media: ffprobe failed for clip {clip_path!r} (index={index}): {exc}"
        ) from exc

    duration_s = max(0.0, float(video_probe.duration_s))
    at_t_s = max(0.0, duration_s * at_frac)

    out_path = os.path.join(work_dir, f"card_{index:02d}.png")
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{at_t_s:.3f}",
        "-i",
        clip_path,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        out_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=FRAME_EXTRACT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"resolve_card_media: ffmpeg timed out extracting frame from {clip_path!r} "
            f"(index={index}, t={at_t_s:.3f}s)"
        ) from exc

    if result.returncode != 0 or not os.path.exists(out_path):
        stderr_excerpt = (result.stderr or "")[-2000:]
        raise RuntimeError(
            f"resolve_card_media: ffmpeg frame extraction failed for {clip_path!r} "
            f"(index={index}, t={at_t_s:.3f}s, rc={result.returncode}): {stderr_excerpt}"
        )

    return CardAsset(index=index, image_path=out_path)
