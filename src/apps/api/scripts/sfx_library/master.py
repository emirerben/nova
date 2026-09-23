"""Mastering + QA that make every library effect iPhone-safe (KRI-173).

The device mixer places an effect at ``at_s`` with no per-clip fade or gain
staging beyond the placement's ``gain``, so the file itself must be clean:

* 48 kHz stereo AAC in ``.m4a`` (iOS ``PlayableAudioFile`` rejects ogg/opus/webm).
* The first audible sample lands within 1 ms of the file start, so the hit
  lands on ``at_s``; a 0.5 ms raised-cosine fade-in keeps it click-free.
* The tail fades to digital silence, so a hard cut never clicks.
* One level target for everything: the loudest 100 ms window sits at
  ``TARGET_LUFS`` (K-weighted), unless that would push the true peak above
  ``TRUE_PEAK_CEILING_DBTP``, in which case the ceiling wins.
* One-shots are at most 3 s, beds (loops, crowds, builds) at most 10 s.

``qa_encoded`` re-decodes the delivered file and re-checks all of it, because
AAC priming and encoder overshoot are only visible after encoding.
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

SR = 48_000
Kind = Literal["one_shot", "bed"]

TARGET_LUFS = -14.0
TRUE_PEAK_CEILING_DBTP = -1.0
MAX_DURATION_S: dict[str, float] = {"one_shot": 3.0, "bed": 10.0}
LEADING_SILENCE_MAX_MS = 10.0
AAC_BITRATE = "192k"

_ONSET_REL_DB = -40.0
_TAIL_REL_DB = -60.0
_FADE_IN_S = 0.0005
_LOUDNESS_WINDOW_S = 0.1
_AUDIBLE_DBFS = -50.0


@dataclass(frozen=True)
class MasterReport:
    duration_s: float
    loudness_lufs: float
    true_peak_dbtp: float
    gain_db: float
    peak_limited: bool
    capped: bool


def _db(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-12))


def _k_weighting_gain(freqs: np.ndarray) -> np.ndarray:
    """|H(f)| of the ITU-R BS.1770 pre-filter + RLB high-pass at 48 kHz."""
    z = np.exp(-2j * np.pi * freqs / SR)
    shelf = (1.53512485958697 - 2.69169618940638 * z + 1.19839281085285 * z**2) / (
        1 - 1.69065929318241 * z + 0.73248077421585 * z**2
    )
    rlb = (1 - 2 * z + z**2) / (1 - 1.99004745483398 * z + 0.99007225036621 * z**2)
    return np.abs(shelf * rlb)


def short_window_loudness(x: np.ndarray) -> float:
    """Loudest 100 ms K-weighted window, in LUFS (BS.1770 channel sum)."""
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    m = n + 4096
    spectrum = np.fft.rfft(x, m, axis=0) * _k_weighting_gain(np.fft.rfftfreq(m, 1 / SR))[:, None]
    weighted = np.fft.irfft(spectrum, m, axis=0)[:n]
    window = max(1, int(_LOUDNESS_WINDOW_S * SR))
    power = np.sum(weighted**2, axis=1)
    if n <= window:
        mean_square = float(np.sum(power)) / window
    else:
        running = np.concatenate([[0.0], np.cumsum(power)])
        mean_square = float(np.max(running[window:] - running[:-window])) / window
    return -0.691 + 10.0 * math.log10(max(mean_square, 1e-20))


def true_peak_dbtp(x: np.ndarray, oversample: int = 4) -> float:
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    m = n + 1024
    up = np.fft.irfft(np.fft.rfft(x, m, axis=0), m * oversample, axis=0) * oversample
    return _db(float(np.max(np.abs(up))))


def _raised_cosine(n: int) -> np.ndarray:
    return 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, n))


def _tail_fade_s(kind: Kind, length_s: float, capped: bool, override_ms: float | None) -> float:
    if override_ms is not None:
        return override_ms / 1000.0
    if capped:
        return 1.0 if kind == "bed" else 0.3
    return min(max(0.15 * length_s, 0.01), 0.5 if kind == "bed" else 0.25)


def master(
    audio: np.ndarray,
    kind: Kind,
    *,
    tail_fade_ms: float | None = None,
) -> tuple[np.ndarray, MasterReport]:
    """Trim, fade, cap and level one effect. Returns float stereo at 48 kHz."""
    x = np.asarray(audio, dtype=np.float64)
    if x.ndim == 1:
        x = np.stack([x, x], axis=1)
    envelope = np.max(np.abs(x), axis=1)
    peak = float(np.max(envelope)) if len(envelope) else 0.0
    if peak <= 0.0:
        raise ValueError("effect is silent")

    onset = int(np.argmax(envelope > peak * 10 ** (_ONSET_REL_DB / 20)))
    last = len(envelope) - 1 - int(np.argmax(envelope[::-1] > peak * 10 ** (_TAIL_REL_DB / 20)))
    x = x[onset : last + int(0.01 * SR) + 1].copy()

    max_samples = int(MAX_DURATION_S[kind] * SR)
    capped = len(x) > max_samples
    x = x[:max_samples]

    fade_in = min(len(x), max(1, int(_FADE_IN_S * SR)))
    x[:fade_in] *= _raised_cosine(fade_in)[:, None]
    fade_out = min(len(x), max(1, int(_tail_fade_s(kind, len(x) / SR, capped, tail_fade_ms) * SR)))
    x[-fade_out:] *= _raised_cosine(fade_out)[::-1, None]
    x[-1] = 0.0

    loudness = short_window_loudness(x)
    peak_db = true_peak_dbtp(x)
    loudness_gain = TARGET_LUFS - loudness
    ceiling_gain = TRUE_PEAK_CEILING_DBTP - peak_db
    gain_db = min(loudness_gain, ceiling_gain)
    x *= 10 ** (gain_db / 20)
    return x, MasterReport(
        duration_s=round(len(x) / SR, 4),
        loudness_lufs=round(loudness + gain_db, 2),
        true_peak_dbtp=round(peak_db + gain_db, 2),
        gain_db=round(gain_db, 2),
        peak_limited=ceiling_gain < loudness_gain,
        capped=capped,
    )


# ── Encode / decode ─────────────────────────────────────────────────────────


def encode_m4a(x: np.ndarray, path: Path) -> None:
    pcm = np.ascontiguousarray(np.asarray(x, dtype=np.float32))
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "pipe:0",
            "-c:a", "aac", "-b:a", AAC_BITRATE, "-ar", str(SR),
            "-movflags", "+faststart", "-map_metadata", "-1", "-fflags", "+bitexact",
            str(path),
        ],
        input=pcm.tobytes(),
        capture_output=True,
        timeout=120,
        check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-500:])


def deliver(x: np.ndarray, kind: Kind, path: Path, max_passes: int = 4) -> dict:
    """Encode, then correct level against the DECODED file and re-encode.

    AAC smears very short transients: a peak-limited kick can overshoot the
    ceiling by ~1.5 dB after encoding, and a 20 ms tap can lose 4 dB of peak.
    Measure what the device will actually play and converge on the same
    target/ceiling rule ``master`` applies before encoding.
    """
    x = np.asarray(x, dtype=np.float64)
    for _ in range(max_passes):
        encode_m4a(x, path)
        decoded = decode_playable(path)
        adjust = min(
            TARGET_LUFS - short_window_loudness(decoded),
            TRUE_PEAK_CEILING_DBTP - true_peak_dbtp(decoded),
        )
        if abs(adjust) < 0.25:
            break
        x = x * 10 ** (adjust / 20)
    return qa_encoded(path, kind)


def decode(path: Path | str, *, channels: int = 2) -> np.ndarray:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", str(channels),
         "-ar", str(SR), "-f", "f32le", "pipe:1"],
        capture_output=True,
        timeout=120,
        check=False,
    )  # fmt: skip
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(f"decode failed for {path}: {result.stderr[-300:]!r}")
    return np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, channels).astype(np.float64)


def _probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=format_name:stream=codec_name,sample_rate,channels,duration_ts,time_base",
         "-of", "json", str(path)],
        capture_output=True,
        timeout=30,
        check=True,
    )  # fmt: skip
    return json.loads(result.stdout)


def decode_playable(path: Path) -> np.ndarray:
    """Decode exactly the samples a player plays.

    ffmpeg honours the edit list's start (AAC priming) but still emits the
    encoder's end padding (up to 1023 samples) past the track duration, which
    AVFoundation does not play. Trim to ``duration_ts`` so tail, loudness and
    peak checks measure the real end of the effect.
    """
    x = decode(path)
    stream = (_probe(path).get("streams") or [{}])[0]
    num, _, den = str(stream.get("time_base") or "").partition("/")
    try:
        playable = round(int(stream["duration_ts"]) * int(num) / int(den) * SR)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return x
    return x[:playable] if 0 < playable < len(x) else x


# ── QA on the delivered file ────────────────────────────────────────────────


def qa_encoded(path: Path, kind: Kind) -> dict:
    """Re-check an encoded effect against the iPhone contract.

    Returns measurements plus ``problems`` (empty when the file passes).
    """
    problems: list[str] = []
    probe = _probe(path)
    stream = (probe.get("streams") or [{}])[0]
    if "mp4" not in probe.get("format", {}).get("format_name", "") or path.suffix != ".m4a":
        problems.append("container is not m4a/mp4")
    if stream.get("codec_name") != "aac":
        problems.append(f"codec {stream.get('codec_name')!r} is not aac")
    if int(stream.get("sample_rate") or 0) != SR:
        problems.append(f"sample rate {stream.get('sample_rate')} is not {SR}")
    if int(stream.get("channels") or 0) != 2:
        problems.append("not stereo")

    x = decode_playable(path)
    envelope = np.max(np.abs(x), axis=1)
    audible = np.flatnonzero(envelope > 10 ** (_AUDIBLE_DBFS / 20))
    leading_ms = float(audible[0]) / SR * 1000 if len(audible) else float("inf")
    duration_s = len(x) / SR
    # A hard cut clicks; a fade that reaches the end does not. Judge the
    # playable file's final 1 ms by its peak.
    tail = x[-int(0.001 * SR) :]
    tail_db = _db(float(np.max(np.abs(tail)))) if len(tail) else -120.0
    loudness = short_window_loudness(x)
    peak_db = true_peak_dbtp(x)

    if leading_ms > LEADING_SILENCE_MAX_MS:
        problems.append(f"leading silence {leading_ms:.1f} ms > {LEADING_SILENCE_MAX_MS} ms")
    if duration_s > MAX_DURATION_S[kind] + 0.05:
        problems.append(f"{kind} is {duration_s:.2f}s > {MAX_DURATION_S[kind]}s")
    if tail_db > -45.0:
        problems.append(f"tail not faded ({tail_db:.1f} dBFS peak in the last 1 ms)")
    if peak_db > TRUE_PEAK_CEILING_DBTP + 0.6:
        problems.append(f"true peak {peak_db:.2f} dBTP over the ceiling")
    if loudness > TARGET_LUFS + 1.0:
        problems.append(f"loudness {loudness:.1f} LUFS above target {TARGET_LUFS}")
    # Very short transients are legitimately peak-limited below the loudness
    # target; anything quiet AND well under the ceiling was mis-levelled.
    if loudness < TARGET_LUFS - 1.5 and peak_db < TRUE_PEAK_CEILING_DBTP - 1.5:
        problems.append(f"loudness {loudness:.1f} LUFS under target with peak headroom left")
    return {
        "codec": stream.get("codec_name"),
        "sample_rate": int(stream.get("sample_rate") or 0),
        "duration_s": round(duration_s, 3),
        "leading_silence_ms": round(leading_ms, 2),
        "tail_peak_dbfs": round(tail_db, 1),
        "loudness_lufs": round(loudness, 2),
        "true_peak_dbtp": round(peak_db, 2),
        "bytes": path.stat().st_size,
        "problems": problems,
    }
