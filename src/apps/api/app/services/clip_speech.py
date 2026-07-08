"""Speech-coverage estimation for the format-aware edit engine (Lane C).

`speech_coverage(path)` returns the fraction of a clip's duration that carries
non-silent audio (0.0 = silent, ~1.0 = continuous speech/voice). The
`talking_head` assembler uses it to pick the spine clip — the one whose audio
track carries the whole video — so a high score is a strong "this is the person
talking" signal.

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
_NOISE_FLOOR_DB = -30
_MIN_SILENCE_S = 0.3

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


def speech_coverage(path: str) -> float:
    """Fraction of `path`'s duration that is non-silent audio, in [0, 1].

    Returns 0.0 (never raises) when the clip has no audio stream, can't be
    probed, or ffmpeg/parsing fails — the safe default that keeps a clip out of
    the talking-head spine.
    """
    try:
        probe = probe_video(path)
    except Exception as exc:  # ProbeError, timeout, anything — stay best-effort.
        log.warning("speech_coverage_probe_failed", path=path, error=str(exc))
        return 0.0

    duration = probe.duration_s
    if duration <= 0:
        return 0.0
    if not probe.has_audio:
        # No audio track at all → no speech. (Distinct from "audio but silent".)
        return 0.0

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
        f"silencedetect=noise={_NOISE_FLOOR_DB}dB:d={_MIN_SILENCE_S}",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except Exception as exc:
        log.warning("speech_coverage_ffmpeg_failed", path=path, error=str(exc))
        return 0.0

    if result.returncode != 0:
        log.warning(
            "speech_coverage_ffmpeg_nonzero",
            path=path,
            stderr=result.stderr.decode(errors="replace")[-200:],
        )
        return 0.0

    stderr_text = result.stderr.decode(errors="replace")
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


def _silent_seconds(stderr_text: str, duration: float) -> float:
    """Sum silent intervals from silencedetect stderr.

    silencedetect emits `silence_start` / `silence_end` markers in order. Pair
    them by index. A file that ends mid-silence has a final `silence_start` with
    no matching `silence_end` — clamp that open interval to `duration`.
    """
    starts = [float(m.group(1)) for m in _SILENCE_START_RE.finditer(stderr_text)]
    ends = [float(m.group(1)) for m in _SILENCE_END_RE.finditer(stderr_text)]

    silent = 0.0
    for i, start in enumerate(starts):
        # silencedetect can report a tiny negative start on lead-in; floor at 0.
        start = max(0.0, start)
        end = ends[i] if i < len(ends) else duration  # unclosed trailing silence
        end = min(end, duration)
        if end > start:
            silent += end - start
    return min(silent, duration)
