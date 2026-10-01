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
import wave
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

# Ambient-adaptive silence (KRI-234). silencedetect compares every SAMPLE to an
# absolute -30 dBFS floor, so rain/wind/traffic transients that peak above it
# break every pause: a night-rain take (job 62716037) produced ZERO spans, rule 3
# never tightened its 0.85 s pause and the 2.8 s silent tail was never trimmed.
# The speech-cleanup callers therefore also read a short-window RMS envelope and
# mark windows quieter than ``floor + margin`` as silent, where ``floor`` is the
# clip's own ambient level. Only clips whose ambient floor sits above
# _ENERGY_ACTIVE_FLOOR_DB (where the absolute floor is unreliable) and whose
# speech clearly stands out (_ENERGY_MIN_SNR_DB) get the extra spans; quiet
# clips keep the plain silencedetect result.
_ENERGY_WINDOW_S = 0.05
_ENERGY_SAMPLE_RATE = 16000
_ENERGY_FLOOR_PERCENTILE = 0.10
_ENERGY_SPEECH_PERCENTILE = 0.90
_ENERGY_ACTIVE_FLOOR_DB = -50.0
_ENERGY_MIN_SNR_DB = 12.0
_ENERGY_MIN_MARGIN_DB = 6.0
_ENERGY_MARGIN_FRAC = 0.35
# Each energy span is pulled this far back from the sound on either side, so a
# cut planned on it keeps a pre-roll before the next onset and a post-roll after
# the last word (window-granular RMS cannot place a boundary closer than one
# window, and ASR start times on noisy footage land late).
_ENERGY_EDGE_GUARD_S = 0.08
_RMS_LEVEL_RE = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(\S+)")
_PTS_TIME_RE = re.compile(r"pts_time:\s*(-?[\d.]+)")

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
    path: str,
    *,
    noise_db: float = _NOISE_FLOOR_DB,
    min_silence_s: float = _MIN_SILENCE_S,
    ambient_adaptive: bool = False,
) -> list[tuple[float, float]]:
    """Merged, sorted (silence_start_s, silence_end_s) ranges for `path`.

    Same best-effort contract as `speech_coverage`: probe failure, missing
    audio stream, ffmpeg failure/timeout, or non-zero exit returns [] rather
    than raising. An unclosed trailing `silence_start` (file ends mid-silence)
    closes at the clip's end. ``ambient_adaptive`` unions in the noise-relative
    energy spans (see _ENERGY_* above) — speech-cleanup callers only.
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
        spans = _merge_intervals(_silence_intervals(stderr_text, duration))
    except (ArithmeticError, TypeError, ValueError):
        log.warning("speech_coverage_parse_failed", path=path)
        return []
    if ambient_adaptive:
        spans = _merge_intervals(
            [*spans, *_ambient_energy_silences(path, duration, min_silence_s=min_silence_s)]
        )
    return spans


def detect_silences_with_status(
    path: str,
    *,
    noise_db: float = _NOISE_FLOOR_DB,
    min_silence_s: float = _MIN_SILENCE_S,
    ambient_adaptive: bool = False,
) -> SilenceDetectionResult:
    """Run the existing probe/FFmpeg pass and retain its bounded outcome.

    This is the status-bearing companion to :func:`detect_silences`; the
    legacy wrapper deliberately continues returning ``[]`` for every failure.
    No retry is performed here. ``ambient_adaptive`` adds one RMS-envelope pass
    whose spans are unioned in; its own failure only drops those extra spans.
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
    if ambient_adaptive:
        spans = tuple(
            _merge_intervals(
                [*spans, *_ambient_energy_silences(path, duration, min_silence_s=min_silence_s)]
            )
        )
    return SilenceDetectionResult(spans=spans, status="ok")


def _ambient_energy_silences(
    path: str, duration: float, *, min_silence_s: float
) -> list[tuple[float, float]]:
    """Noise-relative silence spans from a short-window RMS envelope.

    Best-effort: any ffmpeg/parse failure, an ambient floor quiet enough for
    the absolute silencedetect floor to be trusted, or speech that does not
    clearly rise above the ambient floor returns ``[]``.
    """
    windows = _rms_envelope(path)
    if len(windows) < 10:
        return []
    levels = sorted(level for _t, level in windows)
    floor_db = levels[int(len(levels) * _ENERGY_FLOOR_PERCENTILE)]
    speech_db = levels[min(len(levels) - 1, int(len(levels) * _ENERGY_SPEECH_PERCENTILE))]
    snr_db = speech_db - floor_db
    if floor_db <= _ENERGY_ACTIVE_FLOOR_DB or snr_db < _ENERGY_MIN_SNR_DB:
        return []
    threshold_db = floor_db + max(_ENERGY_MIN_MARGIN_DB, _ENERGY_MARGIN_FRAC * snr_db)
    spans = _energy_spans(windows, duration, threshold_db=threshold_db, min_silence_s=min_silence_s)
    log.info(
        "ambient_silence_spans",
        path=path,
        floor_db=round(floor_db, 1),
        speech_db=round(speech_db, 1),
        threshold_db=round(threshold_db, 1),
        spans=len(spans),
    )
    return spans


def _energy_spans(
    windows: list[tuple[float, float]],
    duration: float,
    *,
    threshold_db: float,
    min_silence_s: float,
) -> list[tuple[float, float]]:
    """Runs of below-threshold windows, edge-guarded where they meet sound."""
    raw: list[tuple[float, float]] = []
    run_start: float | None = None
    for t, level in windows:
        if level < threshold_db:
            if run_start is None:
                run_start = t
        elif run_start is not None:
            raw.append((run_start, t))
            run_start = None
    if run_start is not None:
        raw.append((run_start, duration))

    spans: list[tuple[float, float]] = []
    for start, end in raw:
        start = max(0.0, start)
        end = min(duration, end)
        # Clip edges border no sound, so only interior sides are guarded.
        if start > 0.0:
            start += _ENERGY_EDGE_GUARD_S
        if end < duration:
            end -= _ENERGY_EDGE_GUARD_S
        if end - start >= min_silence_s:
            spans.append((round(start, 3), round(end, 3)))
    return spans


def _rms_envelope(path: str) -> list[tuple[float, float]]:
    """(window_start_s, rms_db) per _ENERGY_WINDOW_S window, mono, or []."""
    samples = int(_ENERGY_SAMPLE_RATE * _ENERGY_WINDOW_S)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        path,
        "-vn",
        "-sn",
        "-dn",
        "-af",
        (
            f"aresample={_ENERGY_SAMPLE_RATE},aformat=channel_layouts=mono,"
            f"asetnsamples=n={samples}:p=0,astats=metadata=1:reset=1,"
            "ametadata=print:key=lavfi.astats.Overall.RMS_level"
        ),
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except Exception as exc:  # timeout or spawn failure — extra spans are optional.
        log.warning("ambient_silence_ffmpeg_failed", path=path, error=str(exc))
        return []
    if result.returncode != 0:
        log.warning("ambient_silence_ffmpeg_nonzero", path=path)
        return []
    try:
        text = result.stderr.decode(errors="replace")
    except (AttributeError, TypeError, UnicodeError):
        return []

    windows: list[tuple[float, float]] = []
    pts: float | None = None
    for line in text.splitlines():
        pts_match = _PTS_TIME_RE.search(line)
        if pts_match:
            pts = float(pts_match.group(1))
            continue
        level_match = _RMS_LEVEL_RE.search(line)
        if level_match and pts is not None:
            try:
                level = float(level_match.group(1))
            except ValueError:
                pts = None
                continue
            # Digital silence reports -inf; clamp so percentiles stay finite.
            windows.append((max(0.0, pts), level if math.isfinite(level) else -120.0))
            pts = None
    return windows


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
        duration = float(probe.duration_s)
        has_audio = bool(probe.has_audio)
    except (TypeError, ValueError):
        return None, "invalid_duration"
    except Exception as exc:  # ProbeError, timeout, anything — stay best-effort.
        # Speech-cleanup preflight deliberately decodes the generation-pinned
        # source to a bounded PCM WAV before detection. `probe_video` rejects
        # that audio-only artifact even though it is the safest detector input.
        # Accept only a structurally valid PCM WAV as the narrow fallback;
        # every other probe failure keeps the historical best-effort result.
        duration = _pcm_wave_duration(path)
        if duration is None:
            log.warning("speech_coverage_probe_failed", path=path, error=str(exc))
            return None, "probe_failed"
        has_audio = True
    if not math.isfinite(duration) or duration <= 0:
        return None, "invalid_duration"
    if not has_audio:
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


def _pcm_wave_duration(path: str) -> float | None:
    """Return duration for a valid audio-only PCM WAV, otherwise ``None``."""

    try:
        with wave.open(path, "rb") as artifact:
            channels = artifact.getnchannels()
            sample_width = artifact.getsampwidth()
            frame_rate = artifact.getframerate()
            frame_count = artifact.getnframes()
    except (EOFError, OSError, wave.Error):
        return None
    if channels <= 0 or sample_width <= 0 or frame_rate <= 0 or frame_count <= 0:
        return None
    duration = frame_count / frame_rate
    return duration if math.isfinite(duration) and duration > 0 else None


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
