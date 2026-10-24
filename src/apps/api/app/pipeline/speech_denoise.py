"""Conservative, time-preserving stationary-noise reduction for phone audio.

Production requests require a pinned ARNNDN model. The basic afftdn filter is
retained only for reference checks. Neither promises perfect source separation.
"""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path


class SpeechDenoiseError(RuntimeError):
    """The optional derivative could not be made safely."""


_PROBE_TIMEOUT_S = 15
_FILTER_TIMEOUT_S = 180
# A PCM/AAC container can report a small encoder-padding difference.  More than
# this would move a source segment and must fail open to the original audio.
_MAX_DURATION_DRIFT_S = 0.050


@dataclass(frozen=True)
class DenoisedAudio:
    path: Path
    duration_s: float
    method: str


@dataclass(frozen=True)
class AudioTiming:
    duration_s: float
    start_time_s: float


def audio_timing(path: Path) -> AudioTiming:
    """Return the first audio stream timing, refusing unknown/non-finite values."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable/argv
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=duration,start_time",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SpeechDenoiseError("audio probe failed") from exc
    if result.returncode:
        raise SpeechDenoiseError("audio probe failed")
    try:
        streams = json.loads(result.stdout).get("streams") or []
        duration = float(streams[0]["duration"])
        start_time = float(streams[0].get("start_time", 0))
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SpeechDenoiseError("audio duration unavailable") from exc
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(start_time):
        raise SpeechDenoiseError("audio duration unavailable")
    return AudioTiming(duration_s=duration, start_time_s=start_time)


def audio_duration_s(path: Path) -> float:
    """Compatibility helper for callers that only need duration."""
    return audio_timing(path).duration_s


def _sha256(path: Path) -> str:
    import hashlib

    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _filter_for_model(model_path: Path | None, expected_sha256: str | None) -> tuple[str, str]:
    if model_path is None:
        # Kept only for synthetic/reference checks. Engine-noise requests must
        # require a configured ARNNDN model at the caller boundary.
        return "afftdn=nr=8:nf=-35:tn=1", "afftdn_reference"
    if not model_path.is_file() or not expected_sha256 or _sha256(model_path) != expected_sha256:
        raise SpeechDenoiseError("noise model is unavailable or changed")
    # Filter arguments are not a shell, but FFmpeg still reserves ':' and '\\'.
    escaped = str(model_path).replace("\\", "\\\\").replace(":", "\\:")
    return f"arnndn=m={escaped}:mix=0.8", "arnndn"


def _impulse_peak(audio_filter: str, index: int, delay: int = 0) -> int:
    """Measure one impulse through the production 48 kHz filter chain."""
    sample_rate, frames = 48_000, 28_800
    with tempfile.TemporaryDirectory(prefix="kria_denoise_alignment_") as directory:
        source = Path(directory) / "impulse.wav"
        output = Path(directory) / "filtered.wav"
        with wave.open(str(source), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)
            samples = [0] * frames
            samples[index] = 30_000
            writer.writeframes(
                b"".join(value.to_bytes(2, "little", signed=True) for value in samples)
            )
        try:
            result = subprocess.run(  # noqa: S603 - fixed executable/argv
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(source),
                    "-af",
                    (
                        f"aresample=48000,apad=pad_len={delay},{audio_filter},"
                        f"atrim=start_sample={delay}:end_sample={frames + delay},"
                        "asetpts=PTS-STARTPTS"
                        if delay
                        else f"aresample=48000,{audio_filter}"
                    ),
                    "-c:a",
                    "pcm_s16le",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=_FILTER_TIMEOUT_S,
                check=False,
            )
            if result.returncode:
                raise SpeechDenoiseError("noise filter alignment check failed")
            with wave.open(str(output), "rb") as reader:
                if reader.getframerate() != sample_rate or reader.getnchannels() != 1:
                    raise SpeechDenoiseError("noise filter alignment check failed")
                raw = reader.readframes(reader.getnframes())
        except (OSError, wave.Error, subprocess.TimeoutExpired) as exc:
            raise SpeechDenoiseError("noise filter alignment check failed") from exc
    values = memoryview(raw).cast("h")
    if not values or max(abs(value) for value in values) < 100:
        raise SpeechDenoiseError("noise filter alignment check failed")
    return max(range(len(values)), key=lambda offset: abs(values[offset]))


def _filter_delay(audio_filter: str) -> int:
    positions = (4_800, 12_000, 19_200)
    delays = [_impulse_peak(audio_filter, index) - index for index in positions]
    if any(delay < 0 or delay > 2_400 for delay in delays) or len(set(delays)) != 1:
        raise SpeechDenoiseError("noise filter delay is not stable")
    delay = delays[0]
    for index in (*positions, 27_000):
        if abs(_impulse_peak(audio_filter, index, delay) - index) > 1:
            raise SpeechDenoiseError("noise filter compensation changed sample alignment")
    return delay


def reduce_stationary_noise(
    source: Path,
    destination: Path,
    *,
    model_path: Path | None = None,
    expected_sha256: str | None = None,
) -> DenoisedAudio:
    """Write an AAC-only derivative with the source audio timeline unchanged.

    Filter latency is measured and compensated without shortening the source.
    Callers must treat any exception as an optional-effect miss.
    """
    input_timing = audio_timing(source)
    # The recipe indexes source audio by the video's timebase. Re-encoding a
    # stream with a nonzero start timestamp would silently move every chosen
    # speech segment, so this optional effect refuses it rather than guessing.
    if abs(input_timing.start_time_s) > 0.005:
        raise SpeechDenoiseError("audio stream has unsupported start offset")
    audio_filter, method = _filter_for_model(model_path, expected_sha256)
    delay = _filter_delay(audio_filter)
    source_samples = round(input_timing.duration_s * 48_000)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable/argv
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-af",
                f"aresample=48000,apad=pad_len={delay},{audio_filter},"
                f"atrim=start_sample={delay}:end_sample={source_samples + delay},"
                "asetpts=PTS-STARTPTS",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-ar",
                "48000",
                "-movflags",
                "+faststart",
                str(destination),
            ],
            capture_output=True,
            text=True,
            timeout=_FILTER_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        destination.unlink(missing_ok=True)
        raise SpeechDenoiseError("noise reduction failed") from exc
    if result.returncode or not destination.is_file() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise SpeechDenoiseError("noise reduction failed")
    output_timing = audio_timing(destination)
    if (
        abs(output_timing.duration_s - input_timing.duration_s) > _MAX_DURATION_DRIFT_S
        or abs(output_timing.start_time_s - input_timing.start_time_s) > 0.005
    ):
        destination.unlink(missing_ok=True)
        raise SpeechDenoiseError("noise reduction changed audio duration")
    return DenoisedAudio(path=destination, duration_s=output_timing.duration_s, method=method)
