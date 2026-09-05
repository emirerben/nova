"""Speech-coverage estimation for the format-aware edit engine (Lane C).

`speech_coverage(path)` returns the fraction of a clip's duration that carries
non-silent audio (0.0 = silent, ~1.0 = continuous speech/voice). The
`talking_head` assembler uses it to pick the spine clip — the one whose audio
track carries the whole video — so a high score is a strong "this is the person
talking" signal.

`detect_silences_with_status(path)` exposes the underlying silence ranges and
a bounded outcome for the silence-cut pipeline (plans/010); the legacy
`detect_silences(path)` wrapper still returns only the merged/sorted list and
still maps every failure to ``[]``. Both share one FFmpeg invocation with
tunable `noise_db`/`min_silence_s`.

Deliberately NOT an LLM signal. It rides the same FFmpeg `silencedetect` path
the beat detector already uses (`_detect_audio_beats` in template_orchestrate),
parsing `silence_start`/`silence_end` pairs from stderr. Best-effort by design:
any probe failure, non-zero ffmpeg exit, or parse error returns 0.0 rather than
raising — a clip we can't measure simply isn't promoted to the spine.

CLAUDE.md anti-pattern guard: subprocess FFmpeg only, never MoviePy.
"""

from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass
from typing import Literal

import structlog

from app.pipeline.probe import probe_video

log = structlog.get_logger()

# -30 dBFS is a forgiving floor: phone-mic dialogue sits well above it while
# room tone / handling noise falls below. d=0.3 ignores sub-300ms gaps so the
# natural micro-pauses between words don't count as silence.
_NOISE_FLOOR_DB = -30.0
_MIN_SILENCE_S = 0.3

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")
_SILENCE_MARKER_RE = re.compile(r"silence_(?P<kind>start|end):\s*(?P<value>-?[\d.]+)")
_SILENCE_MARKER_PREFIX_RE = re.compile(r"silence_(?:start|end):")


@dataclass(frozen=True)
class SilenceDetectionResult:
    """Status-bearing result for the silence-cut detector.

    ``spans=()`` with ``status="ok"`` is a successful calibration result: the
    file contains audio but FFmpeg emitted no silence markers.  Every other
    status is a bounded failure reason.  Keeping the reason out of exception
    text makes this object safe to include in timing-only diagnostics.
    """

    spans: tuple[tuple[float, float], ...]
    status: Literal[
        "ok",
        "probe_failed",
        "invalid_duration",
        "no_audio",
        "ffmpeg_timeout",
        "ffmpeg_failed",
        "ffmpeg_nonzero",
        "parse_failed",
    ]


def detect_silences(
    path: str, *, noise_db: float = _NOISE_FLOOR_DB, min_silence_s: float = _MIN_SILENCE_S
) -> list[tuple[float, float]]:
    """Merged, sorted (silence_start_s, silence_end_s) ranges for `path`.

    Same best-effort contract as `speech_coverage`: probe failure, missing
    audio stream, ffmpeg failure/timeout, or non-zero exit returns [] rather
    than raising. An unclosed trailing `silence_start` (file ends mid-silence)
    closes at the clip's end.
    """
    # Keep this wrapper on the original permissive parser.  In particular, a
    # trailing ``silence_start`` still closes at EOF and malformed marker
    # streams keep their historical index-pairing behavior.  V2 callers use
    # the status-bearing companion below, whose stricter parser must not
    # silently change this legacy API or ``speech_coverage``.
    detected = _run_silencedetect(
        path,
        noise_db=noise_db,
        min_silence_s=min_silence_s,
    )
    if detected is None:
        return []
    stderr_text, duration = detected
    try:
        return _merge_intervals(_silence_intervals(stderr_text, duration))
    except (ArithmeticError, TypeError, ValueError):
        log.warning("speech_coverage_parse_failed", path=path)
        return []


def detect_silences_with_status(
    path: str,
    *,
    noise_db: float = _NOISE_FLOOR_DB,
    min_silence_s: float = _MIN_SILENCE_S,
) -> SilenceDetectionResult:
    """Run the existing probe/FFmpeg pass and retain its bounded outcome.

    This is the status-bearing companion to :func:`detect_silences`; the
    legacy wrapper deliberately continues returning ``[]`` for every failure.
    No retry or second media pass is performed here.
    """
    detected, status = _run_silencedetect_with_status(
        path,
        noise_db=noise_db,
        min_silence_s=min_silence_s,
    )
    if detected is None:
        return SilenceDetectionResult(spans=(), status=status)
    stderr_text, duration = detected
    try:
        spans = tuple(_ordered_silence_intervals(stderr_text, duration))
    except (ArithmeticError, TypeError, ValueError):
        log.warning("speech_coverage_parse_failed", path=path)
        return SilenceDetectionResult(spans=(), status="parse_failed")
    return SilenceDetectionResult(spans=spans, status="ok")


def speech_coverage(path: str) -> float:
    """Fraction of `path`'s duration that is non-silent audio, in [0, 1].

    Returns 0.0 (never raises) when the clip has no audio stream, can't be
    probed, or ffmpeg/parsing fails — the safe default that keeps a clip out of
    the talking-head spine.
    """
    # NOT a detect_silences() call: its [] return conflates "no silences" (full
    # coverage, 1.0) with "probe/ffmpeg failed" (must score 0.0 and stay off
    # the spine), and its merged ranges would re-score pathological overlapping
    # pairs that the pinned `_silent_seconds` arithmetic counts twice.
    detected = _run_silencedetect(path, noise_db=_NOISE_FLOOR_DB, min_silence_s=_MIN_SILENCE_S)
    if detected is None:
        return 0.0
    stderr_text, duration = detected

    try:
        silent_s = _silent_seconds(stderr_text, duration)
    except (ArithmeticError, TypeError, ValueError):
        log.warning("speech_coverage_parse_failed", path=path)
        return 0.0
    coverage = 1.0 - (silent_s / duration)
    # Clamp: a trailing-silence clamp or float drift could nudge it slightly out
    # of range. Coverage is a ranking signal, not a precise measurement.
    coverage = max(0.0, min(1.0, coverage))
    log.info(
        "speech_coverage_done",
        path=path,
        duration=round(duration, 2),
        silent_s=round(silent_s, 2),
        coverage=round(coverage, 3),
    )
    return coverage


def _run_silencedetect(
    path: str, *, noise_db: float, min_silence_s: float
) -> tuple[str, float] | None:
    """Probe `path`, run ffmpeg silencedetect, return (stderr_text, duration_s).

    Returns None on any failure — probe error, zero duration, no audio stream
    (short-circuits before spending an ffmpeg pass), ffmpeg exception/timeout,
    or non-zero exit — so each caller keeps its own failure value
    (speech_coverage → 0.0, detect_silences → []). Log event names predate
    detect_silences and stay `speech_coverage_*` for log continuity.
    """
    detected, _status = _run_silencedetect_with_status(
        path,
        noise_db=noise_db,
        min_silence_s=min_silence_s,
    )
    return detected


def _run_silencedetect_with_status(
    path: str, *, noise_db: float, min_silence_s: float
) -> tuple[
    tuple[str, float] | None,
    Literal[
        "ok",
        "probe_failed",
        "invalid_duration",
        "no_audio",
        "ffmpeg_timeout",
        "ffmpeg_failed",
        "ffmpeg_nonzero",
        "parse_failed",
    ],
]:
    """Internal status-bearing form of :func:`_run_silencedetect`."""
    try:
        probe = probe_video(path)
    except Exception as exc:  # ProbeError, timeout, anything — stay best-effort.
        log.warning("speech_coverage_probe_failed", path=path, error=str(exc))
        return None, "probe_failed"

    try:
        duration = float(probe.duration_s)
    except (TypeError, ValueError):
        return None, "invalid_duration"
    if not math.isfinite(duration) or duration <= 0:
        return None, "invalid_duration"
    if not probe.has_audio:
        # No audio track at all → no speech. (Distinct from "audio but silent".)
        return None, "no_audio"

    cmd = [
        "ffmpeg",
        "-i",
        path,
        # Audio-only decode: silencedetect never reads video, and without -vn
        # ffmpeg decodes the full video stream anyway — 10-50x slower on long
        # phone clips, enough to blow the 60s timeout below (which scores the
        # clip 0.0 and can misroute a genuinely narrated clip set to montage).
        "-vn",
        "-sn",
        "-dn",
        "-af",
        # :g drops the trailing .0 from float defaults so the filter arg stays
        # byte-identical to the pre-parameterized command (noise=-30dB, d=0.3).
        f"silencedetect=noise={noise_db:g}dB:d={min_silence_s:g}",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except subprocess.TimeoutExpired as exc:
        log.warning("speech_coverage_ffmpeg_failed", path=path, error=str(exc))
        return None, "ffmpeg_timeout"
    except Exception as exc:
        log.warning("speech_coverage_ffmpeg_failed", path=path, error=str(exc))
        return None, "ffmpeg_failed"

    if result.returncode != 0:
        try:
            stderr_tail = result.stderr.decode(errors="replace")[-200:]
        except (AttributeError, TypeError, UnicodeError):
            stderr_tail = ""
        log.warning(
            "speech_coverage_ffmpeg_nonzero",
            path=path,
            stderr=stderr_tail,
        )
        return None, "ffmpeg_nonzero"

    try:
        stderr_text = result.stderr.decode(errors="replace")
    except (AttributeError, TypeError, UnicodeError):
        log.warning("speech_coverage_parse_failed", path=path)
        return None, "parse_failed"
    return (stderr_text, duration), "ok"


def _silence_intervals(stderr_text: str, duration: float) -> list[tuple[float, float]]:
    """Raw (start_s, end_s) silence intervals from silencedetect stderr,
    in emission order, unmerged.

    silencedetect emits `silence_start` / `silence_end` markers in order. Pair
    them by index. A file that ends mid-silence has a final `silence_start` with
    no matching `silence_end` — clamp that open interval to `duration`.
    """
    starts = [float(m.group(1)) for m in _SILENCE_START_RE.finditer(stderr_text)]
    ends = [float(m.group(1)) for m in _SILENCE_END_RE.finditer(stderr_text)]

    intervals: list[tuple[float, float]] = []
    for i, start in enumerate(starts):
        # silencedetect can report a tiny negative start on lead-in; floor at 0.
        start = max(0.0, start)
        end = ends[i] if i < len(ends) else duration  # unclosed trailing silence
        end = min(end, duration)
        if end > start:
            intervals.append((start, end))
    return intervals


def _ordered_silence_intervals(stderr_text: str, duration: float) -> list[tuple[float, float]]:
    """Strictly parse a well-formed silencedetect marker stream.

    Unlike the legacy index-pairing parser, this status-bearing parser is an
    ordered state machine.  A detector result is trustworthy only when every
    start has exactly one later end and successive intervals move forward in
    source time.  Orphan ends, nested starts, reversed/overlapping pairs,
    malformed numeric markers, and an unclosed final start all fail closed so
    V2 never reports a broken tool stream as ``status="ok"``.
    """
    marker_matches = list(_SILENCE_MARKER_RE.finditer(stderr_text))
    if len(marker_matches) != len(_SILENCE_MARKER_PREFIX_RE.findall(stderr_text)):
        raise ValueError("malformed silencedetect marker")

    intervals: list[tuple[float, float]] = []
    open_start: float | None = None
    previous_end = 0.0

    for marker in marker_matches:
        value = float(marker.group("value"))
        if not math.isfinite(value):
            raise ValueError("non-finite silencedetect marker")

        if marker.group("kind") == "start":
            if open_start is not None:
                raise ValueError("nested silence_start marker")
            start = max(0.0, value)
            if start < previous_end or start >= duration:
                raise ValueError("silence_start is out of order")
            open_start = start
            continue

        if open_start is None:
            raise ValueError("orphan silence_end marker")
        end = min(value, duration)
        if end <= open_start:
            raise ValueError("silence_end does not follow silence_start")
        intervals.append((open_start, end))
        previous_end = end
        open_start = None

    if open_start is not None:
        raise ValueError("unclosed silence_start marker")
    return intervals


def _silent_seconds(stderr_text: str, duration: float) -> float:
    """Sum silent intervals from silencedetect stderr.

    Sums the RAW (unmerged) intervals so malformed stderr with overlapping
    pairs keeps scoring exactly as it did before detect_silences existed —
    IRON-RULE pin in tests/services/test_clip_speech.py.
    """
    silent = sum(end - start for start, end in _silence_intervals(stderr_text, duration))
    return min(silent, duration)


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sort by start and coalesce overlapping/touching intervals — the
    detect_silences() output contract. Well-formed silencedetect output never
    overlaps (silences are separated by sound); this guards the malformed edge.
    """
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
