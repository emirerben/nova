"""Measured clip brightness, so "skip the very dark clips" can be matched (KRI-680).

Prod thread 133BDDED (2026-10-15, boat kit M1): "Çok karanlık, hiçbir şeyin görünmediği
iki klibi kullanma" never matched a clip. Every clip of that night set was described as
"dark ..." by the analyzer, so the words could not tell a black-sky junk clip from a lit
house on the water. Brightness is the one thing the server can measure: a few frames of
the analysis copy, the mean luma and the share of frames where nothing is visible.

``measure_exposure`` runs next to the clip analysis (``autoplace._analyze_video``) and the
result is stored on the analysis under ``EXPOSURE_KEY``. ``exposure_words`` turns it into a
short phrase the clip record carries (``ClipUnderstanding.visibility``), so the resolver,
the planner and the chat agent read the same measured answer. Both are gated by
``settings.clip_exposure_fact_enabled``; a missing or malformed measurement is "unknown"
and every reader behaves as before.

A measured "very dark" clip is not left out by itself: a dark clip the creator asked for
by name (the first clip, its own sound) stays, because an exclusion never removes a clip
another ask names (``app.kria.clip_scope.excluded_clips``).
"""

from __future__ import annotations

import subprocess
from typing import Any

import structlog

log = structlog.get_logger()

EXPOSURE_KEY = "exposure"
EXPOSURE_VERSION = 1

# Sampled frames are shrunk to this many pixels before counting: enough to see a lit
# window on a black frame, cheap to read.
_SAMPLE_W, _SAMPLE_H = 90, 160
_FRAME_BYTES = _SAMPLE_W * _SAMPLE_H
_MAX_FRAMES = 24
_MIN_KEYFRAMES = 8
_FALLBACK_FPS = 2
_FALLBACK_MAX_S = 120
_TIMEOUT_S = 30

# A pixel at this full-range luma or above is something the eye can make out at night
# (a lit window, a face in torchlight); a frame where under 1% of its pixels reach it
# shows nothing. Set on the M1 kit: the two junk clips and the two dark pier clips have
# 64-100% such frames, the lit night clips at most 38%.
_VISIBLE_LUMA = 48
_VISIBLE_SHARE = 0.01

# Tiers, from the same kit (full-range mean, 0-255): the near-black clips sit at 3.7-4.6,
# the darkest usable night clips (torch on trees, first shore lights) at 8.5-11.7.
_VERY_DARK_MEAN = 8.0
_VERY_DARK_SHARE = 0.5
_DARK_MEAN = 40.0
_DARK_SHARE = 0.25
_BRIGHT_MEAN = 200.0


def _ffmpeg_gray_frames(path: str, *, keyframes_only: bool) -> list[bytes]:
    args = ["ffmpeg", "-nostdin", "-v", "error"]
    if keyframes_only:
        args += ["-skip_frame", "nokey"]
    args += ["-i", path, "-map", "0:v:0", "-an", "-sn", "-dn"]
    scale = f"scale={_SAMPLE_W}:{_SAMPLE_H}:out_range=full,format=gray"
    if keyframes_only:
        args += ["-vf", scale, "-fps_mode", "passthrough"]
    else:
        args += ["-t", str(_FALLBACK_MAX_S), "-vf", f"fps={_FALLBACK_FPS},{scale}"]
    args += ["-frames:v", str(_MAX_FRAMES * 8), "-f", "rawvideo", "-"]
    proc = subprocess.run(args, capture_output=True, timeout=_TIMEOUT_S, check=True)
    data = proc.stdout
    return [
        data[i : i + _FRAME_BYTES] for i in range(0, len(data) - _FRAME_BYTES + 1, _FRAME_BYTES)
    ]


def _spread(frames: list[bytes]) -> list[bytes]:
    """At most ``_MAX_FRAMES`` frames, spread evenly over the clip."""
    if len(frames) <= _MAX_FRAMES:
        return frames
    step = len(frames) / _MAX_FRAMES
    return [frames[int(i * step)] for i in range(_MAX_FRAMES)]


def exposure_from_frames(frames: list[bytes]) -> dict[str, Any] | None:
    """Mean luma (0-255, full range) and near-black frame share of 8-bit gray frames."""
    frames = [f for f in frames if len(f) == _FRAME_BYTES]
    if not frames:
        return None
    total = 0
    near_black = 0
    for frame in frames:
        total += sum(frame)
        visible = sum(1 for value in frame if value >= _VISIBLE_LUMA)
        if visible < _VISIBLE_SHARE * _FRAME_BYTES:
            near_black += 1
    return {
        "version": EXPOSURE_VERSION,
        "mean_luma": round(total / (len(frames) * _FRAME_BYTES), 1),
        "near_black_share": round(near_black / len(frames), 2),
        "frames": len(frames),
    }


def measure_exposure(path: str) -> dict[str, Any] | None:
    """The stored exposure block for one video file, or None. Never raises.

    Reads keyframes only (cheap on any length); a clip with too few keyframes is
    sampled at ``_FALLBACK_FPS`` over its first ``_FALLBACK_MAX_S`` seconds instead.
    """
    try:
        frames = _ffmpeg_gray_frames(path, keyframes_only=True)
        if len(frames) < _MIN_KEYFRAMES:
            frames = _ffmpeg_gray_frames(path, keyframes_only=False)
        return exposure_from_frames(_spread(frames))
    except Exception as exc:  # noqa: BLE001 -- best-effort fact, never fails an analysis
        log.warning("clip_exposure.measure_failed", error_type=type(exc).__name__)
        return None


def exposure_words(raw: object) -> str:
    """The measured brightness as words a model can match, or "" when unknown."""
    if not isinstance(raw, dict) or raw.get("version") != EXPOSURE_VERSION:
        return ""
    try:
        mean = float(raw["mean_luma"])
        share = float(raw["near_black_share"])
    except (KeyError, TypeError, ValueError):
        return ""
    if not (0.0 <= mean <= 255.0 and 0.0 <= share <= 1.0):
        return ""
    numbers = (
        f"measured mean brightness {round(mean)}/255, "
        f"{round(share * 100)}% of sampled frames near-black"
    )
    if mean <= _VERY_DARK_MEAN and share >= _VERY_DARK_SHARE:
        tier = "very dark, almost nothing visible"
    elif mean < _DARK_MEAN or share >= _DARK_SHARE:
        tier = "dark, low light but visible"
    elif mean > _BRIGHT_MEAN:
        tier = "very bright, may look washed out"
    else:
        tier = "normal brightness"
    return f"{tier} ({numbers})"


__all__ = ["EXPOSURE_KEY", "exposure_from_frames", "exposure_words", "measure_exposure"]
