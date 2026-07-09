"""Speech-coverage estimation for the format-aware edit engine (Lane C).

`speech_coverage(path)` returns the fraction of a clip's duration that carries
non-silent audio (0.0 = silent, ~1.0 = continuous speech/voice). The
`talking_head` assembler uses it to pick the spine clip — the one whose audio
track carries the whole video — so a high score is a strong "this is the person
talking" signal.

`detect_silences(path)` exposes the underlying silence ranges (merged, sorted)
for the silence-cut pipeline (plans/010) — same ffmpeg invocation, tunable
`noise_db`/`min_silence_s`, same best-effort contract: any failure returns []
rather than raising.

Deliberately NOT an LLM signal. It rides the same FFmpeg `silencedetect` path
the beat detector already uses (`_detect_audio_beats` in template_orchestrate),
parsing `silence_start`/`silence_end` pairs from stderr. Best-effort by design:
any probe failure, non-zero ffmpeg exit, or parse error returns 0.0 rather than
raising — a clip we can't measure simply isn't promoted to the spine.

CLAUDE.md anti-pattern guard: subprocess FFmpeg only, never MoviePy.
"""

from __future__ import annotations

import re
import subprocess

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


def detect_silences(
    path: str, *, noise_db: float = _NOISE_FLOOR_DB, min_silence_s: float = _MIN_SILENCE_S
) -> list[tuple[float, float]]:
    """Merged, sorted (silence_start_s, silence_end_s) ranges for `path`.

    Same best-effort contract as `speech_coverage`: probe failure, missing
    audio stream, ffmpeg failure/timeout, or non-zero exit returns [] rather
    than raising. An unclosed trailing `silence_start` (file ends mid-silence)
    closes at the clip's end.
    """
    detected = _run_silencedetect(path, noise_db=noise_db, min_silence_s=min_silence_s)
    if detected is None:
        return []
    stderr_text, duration = detected
    return _merge_intervals(_silence_intervals(stderr_text, duration))


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

    silent_s = _silent_seconds(stderr_text, duration)
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
    try:
        probe = probe_video(path)
    except Exception as exc:  # ProbeError, timeout, anything — stay best-effort.
        log.warning("speech_coverage_probe_failed", path=path, error=str(exc))
        return None

    duration = probe.duration_s
    if duration <= 0:
        return None
    if not probe.has_audio:
        # No audio track at all → no speech. (Distinct from "audio but silent".)
        return None

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
    except Exception as exc:
        log.warning("speech_coverage_ffmpeg_failed", path=path, error=str(exc))
        return None

    if result.returncode != 0:
        log.warning(
            "speech_coverage_ffmpeg_nonzero",
            path=path,
            stderr=result.stderr.decode(errors="replace")[-200:],
        )
        return None

    return result.stderr.decode(errors="replace"), duration


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
