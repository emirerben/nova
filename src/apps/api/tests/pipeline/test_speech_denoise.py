from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.pipeline.speech_denoise import audio_timing, reduce_stationary_noise

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg is required"
)


def test_reference_filter_compensates_measured_sample_shift(tmp_path: Path):
    """The reference path is timed, but is not a quality qualification."""
    source = tmp_path / "source.wav"
    output = tmp_path / "denoised.m4a"
    generated = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=120:sample_rate=48000:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1200:sample_rate=48000:duration=3",
            "-filter_complex",
            "[0:a]volume=0.45[engine];[1:a]volume=0.12[speech];[engine][speech]amix=inputs=2",
            "-c:a",
            "pcm_s16le",
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert generated.returncode == 0
    before = audio_timing(source)
    result = reduce_stationary_noise(source, output)
    after = audio_timing(output)
    assert result.method == "afftdn_reference"
    assert result.duration_s == pytest.approx(before.duration_s, abs=0.002)
    assert after.start_time_s == pytest.approx(before.start_time_s, abs=0.005)


def test_rejects_nonzero_input_audio_start_offset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from app.pipeline import speech_denoise

    monkeypatch.setattr(
        speech_denoise, "audio_timing", lambda _: speech_denoise.AudioTiming(1, 0.01)
    )
    with pytest.raises(speech_denoise.SpeechDenoiseError, match="start offset"):
        reduce_stationary_noise(tmp_path / "source.mp4", tmp_path / "output.m4a")
