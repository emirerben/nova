"""Video-frame extraction for carousel cards: rolling video in the small
carousel-card slot, and (for FOCUS-mode choreography) a matching higher-res
"full" tier used once a card zooms to fullscreen.

Companion to `cards.py`'s `resolve_card_media` (single still frame) — this
module extracts a whole JPEG sequence per tier instead, one ffmpeg call per
tier, at `VIDEO_CARD_FPS`. Mirrors `cards.py`'s never-raise-into-a-pipeline-
exception contract: every failure is a plain `RuntimeError`; `segment.py`'s
outer never-raise wrapper is the catch point for callers.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from .cards import resolve_card_media

FRAME_EXTRACT_TIMEOUT_S = 60
VIDEO_CARD_FPS = 30

CARD_TIER_W = 540
CARD_TIER_H = 720
FULL_TIER_W = 1080
FULL_TIER_H = 1920

FRAME_PATTERN = "frame_%04d.jpg"


@dataclass(frozen=True)
class VideoCardAsset:
    index: int
    card_frames_dir: str  # 540x720 cover-cropped JPEG frames at 30fps, frame_%04d.jpg
    card_frame_count: int
    full_frames_dir: str | None  # 1080x1920 cover-cropped JPEGs, only for focus windows
    full_frame_count: int
    poster_path: str  # single PNG (existing resolve_card_media output) as fallback


def resolve_video_card(
    clip_path: str,
    work_dir: str,
    index: int,
    *,
    card_seconds: float,
    full_seconds: float = 0.0,
    start_frac: float = 0.15,
) -> VideoCardAsset:
    """Extract the card tier (always) and the full tier (only when
    `full_seconds > 0`) from `clip_path`, plus the existing still-frame
    poster (`cards.resolve_card_media`) as a renderer fallback.

    Both tiers start at the SAME `at_t_s = clip_duration * start_frac`,
    clamped so `at_t_s + seconds` fits inside the clip when the clip is long
    enough (`at_t_s = min(duration*start_frac, max(0, duration-seconds))`);
    if the clip itself is shorter than `seconds`, `at_t_s` collapses to 0 and
    ffmpeg simply produces fewer frames than requested — callers (the
    renderer) clamp any out-of-range frame index to the last extracted frame
    rather than looping/padding here.

    Raises RuntimeError (never a pipeline-specific exception) on any
    ffprobe/ffmpeg failure — `segment.py`'s never-raise wrapper is the catch
    point for callers.
    """
    from app.pipeline.probe import ProbeError, probe_video  # noqa: PLC0415

    try:
        video_probe = probe_video(clip_path)
    except ProbeError as exc:
        raise RuntimeError(
            f"resolve_video_card: ffprobe failed for clip {clip_path!r} (index={index}): {exc}"
        ) from exc

    duration_s = max(0.0, float(video_probe.duration_s))

    poster = resolve_card_media(clip_path, work_dir, index)

    card_dir = os.path.join(work_dir, f"video_card_{index:02d}_card")
    card_count = _extract_tier(
        clip_path,
        card_dir,
        duration_s=duration_s,
        seconds=max(0.1, card_seconds),
        start_frac=start_frac,
        w=CARD_TIER_W,
        h=CARD_TIER_H,
        index=index,
        tier="card",
    )

    full_dir: str | None = None
    full_count = 0
    if full_seconds > 0:
        full_dir = os.path.join(work_dir, f"video_card_{index:02d}_full")
        full_count = _extract_tier(
            clip_path,
            full_dir,
            duration_s=duration_s,
            seconds=full_seconds,
            start_frac=start_frac,
            w=FULL_TIER_W,
            h=FULL_TIER_H,
            index=index,
            tier="full",
        )

    return VideoCardAsset(
        index=index,
        card_frames_dir=card_dir,
        card_frame_count=card_count,
        full_frames_dir=full_dir,
        full_frame_count=full_count,
        poster_path=poster.image_path,
    )


def _extract_tier(
    clip_path: str,
    out_dir: str,
    *,
    duration_s: float,
    seconds: float,
    start_frac: float,
    w: int,
    h: int,
    index: int,
    tier: str,
) -> int:
    os.makedirs(out_dir, exist_ok=True)

    max_start = max(0.0, duration_s - seconds) if duration_s > 0 else 0.0
    start = max(0.0, min(duration_s * start_frac, max_start))

    out_pattern = os.path.join(out_dir, FRAME_PATTERN)
    # object-fit: cover, matching `renderer._cover_crop`'s CSS semantics —
    # scale the smaller-ratio dimension up to fill, crop the excess on the
    # other axis. ffmpeg's `crop=w:h` centers automatically when x/y are
    # omitted. `-start_number 0` makes frame_0000.jpg the first frame, so a
    # renderer frame index maps directly (no off-by-one).
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{seconds:.3f}",
        "-i",
        clip_path,
        "-vf",
        f"fps={VIDEO_CARD_FPS},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
        "-start_number",
        "0",
        "-q:v",
        "3",
        out_pattern,
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
            f"resolve_video_card: ffmpeg timed out extracting {tier}-tier frames from "
            f"{clip_path!r} (index={index}, start={start:.3f}s, seconds={seconds:.3f}s)"
        ) from exc

    if result.returncode != 0:
        stderr_excerpt = (result.stderr or "")[-2000:]
        raise RuntimeError(
            f"resolve_video_card: ffmpeg {tier}-tier frame extraction failed for "
            f"{clip_path!r} (index={index}, rc={result.returncode}): {stderr_excerpt}"
        )

    frame_count = len(
        [
            name
            for name in os.listdir(out_dir)
            if name.startswith("frame_") and name.endswith(".jpg")
        ]
    )
    if frame_count == 0:
        raise RuntimeError(
            f"resolve_video_card: ffmpeg produced zero {tier}-tier frames for "
            f"{clip_path!r} (index={index})"
        )
    return frame_count
