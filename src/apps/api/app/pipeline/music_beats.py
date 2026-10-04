"""FFmpeg RMS-energy beat detection for music tracks.

Lifted verbatim from ``app/tasks/music_orchestrate.py`` (KRI-374) so the
creator-uploaded-song analysis task and the catalog music task share one
implementation. Behavior is byte-identical to the original private helper.
"""

from __future__ import annotations

import re
import subprocess

import structlog

log = structlog.get_logger()


class BeatDetectionError(RuntimeError):
    """The detector could not run (ffmpeg failed or timed out) -- NOT "no beats found"."""


def detect_music_beats(audio_path: str, min_gap_s: float = 0.15) -> list[float]:
    """Best-effort beats: any detector failure is logged and returns ``[]``.

    The catalog music task keeps this contract (a track with no beats is marked
    failed there). Callers that must tell "the detector broke" from "this audio
    has no beats" use ``detect_music_beats_strict``.
    """
    try:
        return detect_music_beats_strict(audio_path, min_gap_s)
    except BeatDetectionError:
        return []
    except Exception as exc:
        log.warning("music_beat_detect_failed", error=str(exc))
        return []


def detect_music_beats_strict(audio_path: str, min_gap_s: float = 0.15) -> list[float]:
    """Detect beats in a music track via FFmpeg RMS energy peak detection.

    Raises ``BeatDetectionError`` when ffmpeg fails or times out; returns ``[]``
    only when the audio genuinely has no detectable beats.

    Unlike silencedetect (which looks for silence→loud transitions and fails on
    continuous music), this uses per-frame RMS energy from astats and finds local
    peaks above the median energy level. Works for any music with rhythmic content.

    Returns sorted list of beat timestamps in seconds.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-v",
        "error",
        "-i",
        audio_path,
        "-af",
        "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        if result.returncode != 0:
            log.warning("music_beat_detect_ffmpeg_failed", stderr=result.stderr[-500:])
            raise BeatDetectionError("ffmpeg could not read the audio")

        # Parse timestamps and RMS levels from stdout (ametadata file=- writes there)
        lines = result.stdout.strip().split("\n")
        frames: list[tuple[float, float]] = []
        ts = None
        for line in lines:
            line = line.strip()
            if line.startswith("frame:"):
                m = re.search(r"pts_time:([\d.]+)", line)
                if m:
                    ts = float(m.group(1))
            elif "RMS_level" in line and ts is not None:
                m = re.search(r"=(-?\d+\.?\d*)", line)
                if m:
                    try:
                        frames.append((ts, float(m.group(1))))
                    except ValueError:
                        pass  # skip unparseable values like bare "-"

        if len(frames) < 10:
            log.warning("music_beat_detect_too_few_frames", count=len(frames))
            return []

        # Find energy peaks: above median + 3dB, higher than both neighbors
        energies = [e for _, e in frames]
        median_e = sorted(energies)[len(energies) // 2]
        threshold = median_e + 3.0  # 3dB above median

        beats: list[float] = []
        for i in range(1, len(frames) - 1):
            t, e = frames[i]
            _, e_prev = frames[i - 1]
            _, e_next = frames[i + 1]
            if e > e_prev and e > e_next and e > threshold:
                if not beats or (t - beats[-1]) > min_gap_s:
                    beats.append(t)

        log.info("music_beat_detect_done", count=len(beats), threshold=round(threshold, 1))
        return beats

    except (subprocess.SubprocessError, OSError) as exc:
        # A hung/missing ffmpeg is a fault of the run, not a property of the audio.
        # Anything else (a worker's SoftTimeLimitExceeded, a parsing bug) propagates
        # untouched: the strict caller must see it, and `detect_music_beats` is the
        # one that downgrades it to ``[]``.
        log.warning("music_beat_detect_failed", error=str(exc))
        raise BeatDetectionError(str(exc) or exc.__class__.__name__) from exc
