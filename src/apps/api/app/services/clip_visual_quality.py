"""Deterministic visual-quality sampling for uploaded clips.

The clip metadata LLM is good at describing action and energy, but it does not
produce exposure metrics. This module keeps the visual-quality floor boring:
sample luma with FFmpeg signalstats, summarize windows, and return JSON-safe
dicts that downstream matchers can score without another model call.
"""

from __future__ import annotations

import math
import re
import subprocess
from dataclasses import asdict, dataclass

import structlog

log = structlog.get_logger()

SAMPLE_FPS = 1.0
FFMPEG_TIMEOUT_S = 30

# Mean luma (Y channel, 0-255). 35 mirrors the poster extractor's near-black
# floor; 60 is the empirical "this reads too dim in final music output" line
# from the 2026-06-01 dark-clip investigation.
VERY_DARK_LUMA = 35.0
DARK_LUMA = 60.0
VERY_DARK_RATIO = 0.5
DARK_RATIO = 0.5

_FRAME_RE = re.compile(r"frame:\s*\d+.*pts_time:([-\d.]+)")
_YAVG_RE = re.compile(r"lavfi\.signalstats\.YAVG=([-\d.]+)")


class VisualQualityError(RuntimeError):
    """FFmpeg failed or emitted no parseable luma samples."""


@dataclass(frozen=True)
class LumaSample:
    t_s: float
    luma_mean: float


@dataclass(frozen=True)
class VisualQuality:
    classification: str
    sample_count: int
    luma_mean: float | None = None
    luma_min: float | None = None
    luma_max: float | None = None
    luma_stddev: float | None = None
    dark_frame_ratio: float = 0.0
    very_dark_frame_ratio: float = 0.0
    unavailable_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def sample_luma_timeline(
    local_video_path: str,
    *,
    sample_fps: float = SAMPLE_FPS,
    timeout_s: int = FFMPEG_TIMEOUT_S,
) -> list[LumaSample]:
    """Sample per-frame luma from a video at low FPS.

    Uses FFmpeg's `signalstats` + `metadata=print` path so we parse stable
    frame metadata rather than human-oriented progress output.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-i",
        local_video_path,
        "-map",
        "0:v:0",
        "-vf",
        f"fps={sample_fps:g},signalstats,metadata=print:key=lavfi.signalstats.YAVG",
        "-an",
        "-sn",
        "-dn",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise VisualQualityError(f"ffmpeg timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise VisualQualityError(f"ffmpeg failed to start: {exc}") from exc

    stderr_text = result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0:
        raise VisualQualityError(
            f"ffmpeg returned {result.returncode}: {stderr_text[:500]!r}"
        )

    samples = _parse_signalstats_metadata(stderr_text)
    if not samples:
        raise VisualQualityError("signalstats output contained no YAVG samples")
    return samples


def annotate_moments(
    moments: list[dict],
    samples: list[LumaSample],
) -> None:
    """Mutate moment dicts in place with `visual_quality` summaries."""
    for moment in moments:
        if not isinstance(moment, dict):
            continue
        start_s = _float_or(moment.get("start_s"), 0.0)
        end_s = _float_or(moment.get("end_s"), start_s)
        moment["visual_quality"] = summarize_window(samples, start_s, end_s).to_dict()


def summarize_samples(samples: list[LumaSample]) -> VisualQuality:
    if not samples:
        return unavailable_quality("no luma samples")
    values = [s.luma_mean for s in samples]
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    dark_ratio = sum(1 for v in values if v < DARK_LUMA) / len(values)
    very_dark_ratio = sum(1 for v in values if v < VERY_DARK_LUMA) / len(values)
    return VisualQuality(
        classification=_classify(mean, dark_ratio, very_dark_ratio),
        sample_count=len(values),
        luma_mean=round(mean, 3),
        luma_min=round(min(values), 3),
        luma_max=round(max(values), 3),
        luma_stddev=round(math.sqrt(variance), 3),
        dark_frame_ratio=round(dark_ratio, 3),
        very_dark_frame_ratio=round(very_dark_ratio, 3),
    )


def summarize_window(
    samples: list[LumaSample],
    start_s: float,
    end_s: float,
) -> VisualQuality:
    if not samples:
        return unavailable_quality("no luma samples")
    if end_s <= start_s:
        end_s = start_s + 0.001
    in_window = [s for s in samples if start_s <= s.t_s < end_s]
    if not in_window:
        center = (start_s + end_s) / 2.0
        nearest = min(samples, key=lambda s: abs(s.t_s - center))
        in_window = [nearest]
    return summarize_samples(in_window)


def best_quality_window(
    samples: list[LumaSample],
    *,
    clip_dur: float,
    window_dur: float,
) -> tuple[float, VisualQuality]:
    """Choose the brightest viable start for a fallback moment window."""
    if not samples:
        return 0.0, unavailable_quality("no luma samples")
    if clip_dur <= 0:
        return 0.0, summarize_samples(samples)
    window_dur = max(0.001, min(window_dur, clip_dur))
    max_start = max(0.0, clip_dur - window_dur)
    starts = {0.0, max_start}
    starts.update(max(0.0, min(max_start, s.t_s)) for s in samples)

    def score(start_s: float) -> tuple[float, float, float]:
        q = summarize_window(samples, start_s, start_s + window_dur)
        return (_classification_rank(q.classification), q.luma_mean or 0.0, -start_s)

    best_start = max(starts, key=score)
    return round(best_start, 3), summarize_window(samples, best_start, best_start + window_dur)


def unavailable_quality(reason: str) -> VisualQuality:
    return VisualQuality(
        classification="unavailable",
        sample_count=0,
        unavailable_reason=reason[:200],
    )


def _parse_signalstats_metadata(stderr_text: str) -> list[LumaSample]:
    samples: list[LumaSample] = []
    current_t: float | None = None
    for line in stderr_text.splitlines():
        frame_match = _FRAME_RE.search(line)
        if frame_match:
            try:
                current_t = float(frame_match.group(1))
            except ValueError:
                current_t = None
            continue
        yavg_match = _YAVG_RE.search(line)
        if yavg_match and current_t is not None:
            try:
                samples.append(
                    LumaSample(
                        t_s=round(current_t, 3),
                        luma_mean=float(yavg_match.group(1)),
                    )
                )
            except ValueError:
                log.debug("clip_visual_quality_bad_yavg", line=line)
            current_t = None
    return samples


def _classify(mean: float, dark_ratio: float, very_dark_ratio: float) -> str:
    if mean < VERY_DARK_LUMA or very_dark_ratio >= VERY_DARK_RATIO:
        return "very_dark"
    if mean < DARK_LUMA or dark_ratio >= DARK_RATIO:
        return "low_light"
    return "usable"


def _classification_rank(classification: str) -> float:
    return {
        "usable": 3.0,
        "low_light": 1.0,
        "very_dark": 0.0,
        "unavailable": -1.0,
    }.get(classification, -1.0)


def _float_or(value: object, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback
