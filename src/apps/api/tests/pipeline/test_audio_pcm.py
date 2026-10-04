"""decode_pcm_f32: ffmpeg -> mono float32 PCM (shared by SFX analysis + song aligner)."""

from __future__ import annotations

import shutil
import wave

import numpy as np
import pytest

from app.pipeline.audio_pcm import decode_pcm_f32

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _write_wav(path, samples: np.ndarray, sr: int, channels: int = 1) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def test_decodes_resamples_and_downmixes(tmp_path):
    sr_in = 44_100
    t = np.arange(sr_in) / sr_in
    mono = 0.5 * np.sin(2 * np.pi * 440 * t)
    stereo = np.stack([mono, mono], axis=1).reshape(-1)
    path = tmp_path / "tone.wav"
    _write_wav(path, stereo, sr_in, channels=2)

    pcm = decode_pcm_f32(str(path), 16_000)

    assert pcm.dtype == np.float32
    assert abs(pcm.shape[0] - 16_000) <= 64
    assert 0.3 < float(np.abs(pcm).max()) <= 1.0


def test_unreadable_input_returns_empty(tmp_path):
    bad = tmp_path / "nope.wav"
    bad.write_bytes(b"not audio")
    assert decode_pcm_f32(str(bad)).shape == (0,)
    assert decode_pcm_f32(str(tmp_path / "missing.wav")).shape == (0,)


def test_sfx_pcm_samples_delegates_to_shared_decoder(tmp_path):
    from app.services.sfx_analysis import _pcm_samples

    path = tmp_path / "tone.wav"
    _write_wav(path, 0.4 * np.sin(2 * np.pi * 220 * np.arange(16_000) / 16_000), 16_000)
    samples = _pcm_samples(str(path))
    assert isinstance(samples, list) and isinstance(samples[0], float)
    assert abs(len(samples) - 16_000) <= 64
