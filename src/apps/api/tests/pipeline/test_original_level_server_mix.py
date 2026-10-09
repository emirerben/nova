"""The cloud renderer honours the editor's ``mix.original_level`` (real ffmpeg).

Footage carries an 880 Hz tone and the song a 440 Hz tone, so the footage-to-song
ratio of the rendered output is directly measurable per frequency.
"""

from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest

from app.pipeline.authored_timeline import _apply_original_audio_level
from app.tasks import template_orchestrate as to

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")

FOOTAGE_HZ = 880
SONG_HZ = 440


def _run(*args: str) -> bytes:
    return subprocess.run(args, check=True, capture_output=True, timeout=60).stdout


def _tone_mp4(path, frequency: int, *, video: bool) -> str:
    args = ["ffmpeg", "-y"]
    if video:
        args += ["-f", "lavfi", "-i", "color=c=black:s=64x64:r=30"]
    args += ["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration=4"]
    args += ["-t", "4"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"] if video else []
    args += ["-c:a", "aac", str(path)]
    _run(*args)
    return str(path)


def _samples(path) -> np.ndarray:
    raw = _run(
        "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
        "-ar", "48000", "-f", "f32le", "-",
    )  # fmt: skip
    return np.frombuffer(raw, dtype="<f4")


def _amp(samples: np.ndarray, frequency: int) -> float:
    window = samples[48000 : 3 * 48000].astype(np.float64)  # steady 2s middle
    phase = np.arange(len(window)) * (2 * np.pi * frequency / 48000)
    return 2 * abs(np.dot(window, np.exp(-1j * phase))) / len(window)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("orig-level")
    return {
        "video": _tone_mp4(d / "footage.mp4", FOOTAGE_HZ, video=True),
        "song": _tone_mp4(d / "song.m4a", SONG_HZ, video=False),
        "dir": d,
    }


def _mix(media, monkeypatch, name: str, **kwargs) -> np.ndarray:
    monkeypatch.setattr(to, "download_to_file", lambda _src, dst: shutil.copy(media["song"], dst))
    out = media["dir"] / f"{name}.mp4"
    to._mix_template_audio(
        media["video"], "music/song.m4a", str(out), str(media["dir"]), require_audio=True, **kwargs
    )
    return _samples(out)


def test_song_variant_original_level_changes_the_mix(media, monkeypatch):
    replaced = _mix(media, monkeypatch, "replaced")
    full = _mix(media, monkeypatch, "full", original_level=1.0)
    quiet = _mix(media, monkeypatch, "quiet", original_level=0.25)
    muted = _mix(media, monkeypatch, "muted", original_level=0.0)

    ratio = lambda s: _amp(s, FOOTAGE_HZ) / _amp(s, SONG_HZ)  # noqa: E731
    # Unset keeps today's behaviour: the song replaces the footage's sound.
    assert ratio(replaced) < 0.02
    assert ratio(muted) < 0.02
    # The creator's level sets the footage-to-song balance.
    assert ratio(full) > 0.5
    assert ratio(quiet) == pytest.approx(ratio(full) * 0.25, rel=0.15)
    assert _amp(quiet, SONG_HZ) > 0.01  # the song is still there


def test_original_audio_variant_level_scales_the_footage_sound(media, tmp_path):
    full = _samples(media["video"])
    out = _apply_original_audio_level(media["video"], str(tmp_path / "half.mp4"), 0.5)
    half = _samples(out)
    assert _amp(half, FOOTAGE_HZ) == pytest.approx(_amp(full, FOOTAGE_HZ) * 0.5, rel=0.1)
    silent = _samples(_apply_original_audio_level(media["video"], str(tmp_path / "z.mp4"), 0.0))
    assert float(np.max(np.abs(silent))) < 0.001
