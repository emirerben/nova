"""Decode any media file to mono float32 PCM via ffmpeg.

Shared by the sound-effect analyzer and the song aligner (KRI-374). Pure
subprocess + numpy: no network, DB or storage access.
"""

from __future__ import annotations

import subprocess

import numpy as np

DEFAULT_SAMPLE_RATE = 16_000


def decode_pcm_f32(
    path: str,
    sr: int = DEFAULT_SAMPLE_RATE,
    *,
    timeout_s: float = 60.0,
) -> np.ndarray:
    """Return ``path``'s audio as mono float32 samples at ``sr`` Hz.

    Returns an empty array when ffmpeg exits non-zero or produces no audio
    (missing audio stream, unreadable file). A hung ffmpeg raises
    ``subprocess.TimeoutExpired`` and a missing ffmpeg binary raises
    ``FileNotFoundError`` -- both are deployment faults, not bad media, so they
    are not swallowed.
    """
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
            str(sr),
            "-f",
            "f32le",
            "pipe:1",
        ],
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return np.zeros(0, dtype=np.float32)
    usable = len(result.stdout) - (len(result.stdout) % 4)
    return np.frombuffer(result.stdout[:usable], dtype="<f4").astype(np.float32, copy=True)
