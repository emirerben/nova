"""apply_speech_cleanup_to_audio: cuts crossfade instead of hard-splicing.

Real FFmpeg on lavfi pink noise (media files are never committed). Steady
noise stands in for room tone (rain, traffic): a cut that dips, gaps or
fades to silence shows up as a level drop at the join.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.pipeline.speech_cleanup_apply import (
    apply_speech_cleanup_to_audio,
    hydrate_speech_cleanup_snapshot,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
pytestmark = pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "speech_cleanup"
    / "guided_voiceover_mixed_gap_v2.json"
)
RATE = 48000
RMS_WINDOW = RATE * 5 // 1000  # 5 ms


def write_pink_noise(path: Path, *, duration_s: float, sample_rate: int, codec: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=color=pink:seed=1729:amplitude=0.25"
            f":sample_rate={sample_rate}:duration={duration_s}",
            "-ac",
            "1",
            "-c:a",
            codec,
            "-y",
            str(path),
        ],
        check=True,
    )


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (RATE, 1, 2)
        samples = np.frombuffer(wav.readframes(wav.getnframes()), "<i2")
    return samples.astype(np.float64) / 32768


def rms_db(samples: np.ndarray) -> float:
    return float(20 * np.log10(max(float(np.sqrt(np.mean(samples**2))), 1e-9)))


def sample_spans(snapshot) -> list[tuple[int, int]]:  # noqa: ANN001
    return [
        (
            round((snapshot.window_start_s + start_s) * RATE),
            round((snapshot.window_start_s + end_s) * RATE),
        )
        for start_s, end_s in snapshot.cut_plan.keep_segments
    ]


def test_steady_room_tone_keeps_its_level_through_every_cut(tmp_path: Path) -> None:
    """The reported voiceover's plan (8 cuts plus a leading and a trailing trim)
    on a 44.1 kHz AAC source, like a phone voiceover."""
    snapshot = hydrate_speech_cleanup_snapshot(json.loads(FIXTURE_PATH.read_text())["snapshot"])
    source = tmp_path / "voiceover.m4a"
    write_pink_noise(source, duration_s=snapshot.window_end_s, sample_rate=44100, codec="aac")

    cleaned = read_wav(
        Path(apply_speech_cleanup_to_audio(snapshot, str(source), str(tmp_path / "cut.wav")))
    )

    keep = snapshot.cut_plan.keep_segments
    spans = sample_spans(snapshot)
    # Exactly the keep segments' length: every crossfade overlap gives back the
    # handles it borrowed, so the remapped words still land where they play.
    assert len(cleaned) == sum(end - first for first, end in spans)
    assert len(cleaned) / RATE == pytest.approx(
        sum(end_s - start_s for start_s, end_s in keep), abs=len(keep) / RATE
    )
    windows = cleaned[: len(cleaned) // RMS_WINDOW * RMS_WINDOW].reshape(-1, RMS_WINDOW)
    median_db = float(
        np.median(20 * np.log10(np.maximum(np.sqrt(np.mean(windows**2, axis=1)), 1e-9)))
    )
    joints = np.cumsum([end - first for first, end in spans])[:-1]
    assert len(joints) == 8
    for joint in joints:
        # Every 5 ms window (1 ms hop) within 50 ms of the joint. Fading each
        # side to silence dipped 15-20 dB here; a hard splice can click.
        levels = [
            rms_db(cleaned[start : start + RMS_WINDOW])
            for start in range(
                joint - RATE // 20, joint + RATE // 20 - RMS_WINDOW + 1, RATE // 1000
            )
        ]
        assert max(abs(level - median_db) for level in levels) <= 8.0, (joint, levels)


def reference_cut(
    source: np.ndarray,
    spans: list[tuple[int, int]],
    handles: list[int],
    *,
    fade_in: int,
    fade_out: int,
) -> np.ndarray:
    """Each cut's 2*handle samples from both sides mixed under cos/sin gains
    (equal power: they sum to 1 in power); linear declicks at trims."""

    out = np.zeros(0)
    for i, (first, end) in enumerate(spans):
        tail = handles[i + 1] if i + 1 < len(spans) else 0
        piece = source[first - handles[i] : end + tail].copy()
        if i == 0 and fade_in:
            piece[:fade_in] *= np.arange(fade_in) / fade_in
        if i == len(spans) - 1 and fade_out:
            piece[-fade_out:] *= np.arange(fade_out, 0, -1) / fade_out
        overlap = 2 * handles[i]
        if overlap:
            angle = np.pi / 2 * np.arange(overlap) / overlap
            mixed = out[-overlap:] * np.cos(angle) + piece[:overlap] * np.sin(angle)
            out = np.concatenate([out[:-overlap], mixed, piece[overlap:]])
        else:
            out = np.concatenate([out, piece])
    return out


@pytest.mark.parametrize(
    ("keep_segments", "handles", "fade_in", "fade_out"),
    [
        pytest.param(
            # 10 ms removed: each side borrows half of it (5 ms, not 25 ms).
            # No gap: a plain join. Then a 40 ms segment: a quarter of it.
            [(0.10, 1.00), (1.01, 1.50), (1.50, 2.00), (2.40, 2.44)],
            [0, 240, 0, 480],
            576,
            576,
            id="clamped_handles",
        ),
        pytest.param(
            # 12 ms segments between 3 ms gaps: a segment's whole widened span
            # arrives in one decoded frame. A chain of acrossfade (FFmpeg 6.1
            # in CI, 7.1 in production) ended its output early here.
            [(0.10, 0.50), (0.503, 0.515), (0.518, 0.530), (0.533, 1.20)],
            [0, 72, 72, 72],
            576,
            576,
            id="sub_frame_segments",
        ),
        pytest.param([(0.25, 2.75)], [0], 576, 576, id="one_segment"),
        pytest.param([(0.0, 1.0), (1.5, 3.0)], [0, 1200], 0, 0, id="whole_window_edges"),
    ],
)
def test_cut_is_the_equal_power_crossfade_of_the_kept_spans(
    tmp_path: Path,
    keep_segments: list[tuple[float, float]],
    handles: list[int],
    fade_in: int,
    fade_out: int,
) -> None:
    """Sample-exact against a reference render. The window starts 0.5 s into
    the source, as an embedded spine's can; the window's own start and end
    are not cuts, so only a trim inside it fades."""
    snapshot = SimpleNamespace(
        window_start_s=0.5,
        window_end_s=3.5,
        cut_plan=SimpleNamespace(keep_segments=keep_segments),
    )
    source_path = tmp_path / "source.wav"
    write_pink_noise(source_path, duration_s=4.0, sample_rate=RATE, codec="pcm_s16le")

    cleaned = read_wav(
        Path(apply_speech_cleanup_to_audio(snapshot, str(source_path), str(tmp_path / "cut.wav")))
    )

    expected = reference_cut(
        read_wav(source_path),
        sample_spans(snapshot),
        handles,
        fade_in=fade_in,
        fade_out=fade_out,
    )
    assert len(cleaned) == len(expected)
    assert np.max(np.abs(cleaned - expected)) <= 2 / 32768
