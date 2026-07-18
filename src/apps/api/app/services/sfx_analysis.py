"""Deterministic DSP metadata for the curated sound-effects library.

The analyzer intentionally does not pretend that spectral heuristics can prove
an asset is voice-free. ``contains_voice`` remains unknown until a trusted seed
or a human audit sets it; Smart selection treats explicit voice as a hard ban
and can require an approved audit in the creator preset.
"""

from __future__ import annotations

import array
import hashlib
import math
import re
import subprocess

ANALYSIS_VERSION = "sfx-dsp-2026-07-18.1"
_SAMPLE_RATE = 16_000
_WINDOW_SAMPLES = 160


def _pcm_samples(path: str) -> list[float]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(_SAMPLE_RATE),
            "-f",
            "f32le",
            "pipe:1",
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return []
    samples = array.array("f")
    samples.frombytes(result.stdout)
    return list(samples)


def _loudness(path: str) -> tuple[float | None, float | None]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            path,
            "-filter_complex",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    text = result.stderr.decode("utf-8", errors="replace")
    integrated = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s+LUFS", text)
    true_peak = re.findall(r"Peak:\s*(-?\d+(?:\.\d+)?)\s+dBFS", text)
    return (
        float(integrated[-1]) if integrated else None,
        float(true_peak[-1]) if true_peak else None,
    )


def analyze_sound_effect(path: str) -> dict[str, float | str | None]:
    """Return bounded, reproducible audio-shape metadata for one local asset."""

    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    samples = _pcm_samples(path)
    integrated_lufs, true_peak_dbtp = _loudness(path)
    if not samples:
        return {
            "sha256": sha256,
            "analysis_version": ANALYSIS_VERSION,
            "integrated_lufs": integrated_lufs,
            "true_peak_dbtp": true_peak_dbtp,
            "attack_ms": None,
            "decay_ms": None,
            "energy": None,
            "brightness": None,
        }

    envelopes = [
        math.sqrt(
            sum(value * value for value in samples[index : index + _WINDOW_SAMPLES])
            / _WINDOW_SAMPLES
        )
        for index in range(0, len(samples) - _WINDOW_SAMPLES + 1, _WINDOW_SAMPLES)
    ]
    peak = max(envelopes, default=0.0)
    active = [index for index, value in enumerate(envelopes) if value >= peak * 0.1]
    peak_index = envelopes.index(peak) if envelopes else 0
    attack_ms = max(0.0, (peak_index - active[0]) * 10.0) if active else None
    decay_ms = max(0.0, (active[-1] - peak_index) * 10.0) if active else None
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    zero_crossings = sum(
        1 for left, right in zip(samples, samples[1:]) if (left < 0 <= right) or (left >= 0 > right)
    )
    brightness = min(1.0, zero_crossings / max(1.0, len(samples) * 0.35))
    return {
        "sha256": sha256,
        "analysis_version": ANALYSIS_VERSION,
        "integrated_lufs": integrated_lufs,
        "true_peak_dbtp": true_peak_dbtp,
        "attack_ms": round(attack_ms, 2) if attack_ms is not None else None,
        "decay_ms": round(decay_ms, 2) if decay_ms is not None else None,
        "energy": round(min(1.0, rms * 4.0), 5),
        "brightness": round(brightness, 5),
    }
