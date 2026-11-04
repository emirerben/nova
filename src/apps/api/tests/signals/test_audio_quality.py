"""audio_quality against the committed calibration clips and the failure modes it must survive.

The clips are synthetic (see scripts/clip_signal_calibration.py): they prove the measurement
separates the failure modes, not that it works on real phone audio.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from app.services import clip_signals
from app.services.clip_signals import audio_quality
from app.services.clip_signals.registry import SignalInputs

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/clip_signals/audio"
SCRIPT = Path(__file__).resolve().parents[5] / "scripts/clip_signal_calibration.py"
NOISY = {"wind", "crowd"}


def _labels() -> dict[str, str]:
    raw = json.loads((FIXTURES / "labels.json").read_text())
    return {name: entry["label"] for name, entry in raw.items()}


def _record(path: Path | str) -> dict | None:
    out = audio_quality.compute(SignalInputs(media_id="m", kind="video", local_path=str(path)))
    return None if out is None else dict(out)


@pytest.fixture(scope="module")
def records() -> dict[str, dict]:
    out = {}
    for name in _labels():
        rec = _record(FIXTURES / name)
        assert rec is not None, f"{name} produced no signal"
        out[name] = rec
    return out


def test_mute_precision_clears_the_bar_on_the_calibration_set(records):
    labels = _labels()
    flagged = [n for n, r in records.items() if r["noisy"]]
    true_positive = [n for n in flagged if labels[n] in NOISY]
    assert flagged and len(true_positive) / len(flagged) >= 0.9
    assert len(true_positive) / sum(1 for v in labels.values() if v in NOISY) >= 0.8


def test_bass_music_speech_clipping_and_silence_are_never_noise(records):
    labels = _labels()
    for name, rec in records.items():
        if labels[name] in {"music", "clean", "clipped", "silence"}:
            assert not rec["noisy"], f"{name} ({labels[name]}) flagged noisy: {rec['evidence']}"


def test_clipping_and_silence_are_reported_without_becoming_noise(records):
    labels = _labels()
    for name, rec in records.items():
        if labels[name] == "clipped":
            assert rec["clipped"] and rec["clipping"] > 0.01, name
        elif labels[name] != "silence":
            assert not rec["clipped"], name
    zeros = records["silence_00.m4a"]
    assert zeros["silent"] and zeros["wind_score"] == 0.0 and zeros["noise_floor_db"] == -90.0


def test_stereo_and_mono_clips_read_the_same_way(records):
    raw = json.loads((FIXTURES / "labels.json").read_text())
    by_channels: dict[int, list[bool]] = {1: [], 2: []}
    for name, entry in raw.items():
        if entry["label"] in NOISY:
            by_channels[entry["params"]["channels"]].append(records[name]["noisy"])
    assert by_channels[1] and by_channels[2]
    for flags in by_channels.values():
        assert sum(flags) / len(flags) >= 0.7


def test_thresholds_in_the_module_still_match_a_refit_of_the_fixtures(monkeypatch):
    spec = importlib.util.spec_from_file_location("clip_signal_calibration", SCRIPT)
    script = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, script)  # dataclasses resolve annotations by module
    spec.loader.exec_module(script)
    fresh = script.fit(script.fit_rows(types.SimpleNamespace(include_real=False, real_dir=None)))
    assert script._close(fresh, audio_quality.FITTED), (
        "FITTED drifted from the fixtures: run scripts/clip_signal_calibration.py fit --write "
        "and bump SIGNAL_VERSION"
    )


def test_real_speech_voices_stay_clean_at_every_committed_gain():
    clips = sorted((FIXTURES / "tts").glob("*.m4a"))
    assert len(clips) >= 9
    for clip in clips:
        rec = _record(clip)
        assert rec is not None and not rec["noisy"] and not rec["silent"], (clip.name, rec)


@pytest.mark.parametrize("name", ["clean_00.m4a", "clean_07.m4a", "wind_00.m4a", "crowd_00.m4a"])
@pytest.mark.parametrize("gain_db", [-12, 12])
def test_recording_loudness_does_not_move_the_verdict(tmp_path, name, gain_db):
    original = _record(FIXTURES / name)
    out = tmp_path / "gained.m4a"
    _ffmpeg(
        "-i", str(FIXTURES / name), "-af", f"volume={gain_db}dB,alimiter=limit=0.95",
        "-c:a", "aac", "-b:a", "64k", str(out),
    )  # fmt: skip
    gained = _record(out)
    assert original is not None and gained is not None
    assert gained["noisy"] == original["noisy"], (name, gain_db, original, gained)
    assert abs(gained["crowd_score"] - original["crowd_score"]) < 0.15
    assert abs(gained["wind_score"] - original["wind_score"]) < 0.15


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def test_a_video_with_no_audio_stream_has_no_signal(tmp_path):
    path = tmp_path / "silent_video.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=64x64:d=2", "-an", str(path))
    assert _record(path) is None


def test_a_clip_too_short_to_judge_has_no_signal(tmp_path):
    path = tmp_path / "short.wav"
    _ffmpeg("-f", "lavfi", "-i", "sine=f=300:d=0.4", str(path))
    assert _record(path) is None


def test_unreadable_or_missing_media_has_no_signal(tmp_path):
    junk = tmp_path / "junk.m4a"
    junk.write_bytes(b"\x00not audio" * 200)
    assert _record(junk) is None
    assert _record(tmp_path / "absent.m4a") is None


def test_a_missing_ffmpeg_binary_is_a_missing_signal_not_a_crash(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert _record(FIXTURES / "clean_00.m4a") is None


def test_images_and_pathless_inputs_are_skipped():
    assert audio_quality.compute(SignalInputs(media_id="m", kind="image", local_path="x")) is None
    assert audio_quality.compute(SignalInputs(media_id="m", kind="video")) is None


def test_registry_stores_a_current_version_record_for_a_real_clip():
    spec = clip_signals.registry.get("audio_quality")
    assert spec is not None and spec.implemented and spec.version == audio_quality.SIGNAL_VERSION
    inputs = SignalInputs(
        media_id="m", kind="video", local_path=str(FIXTURES / "wind_00.m4a"), duration_s=3.0
    )
    computed = clip_signals.compute_signals(inputs, only=frozenset({"audio_quality"}))
    merged = clip_signals.merge_signals({"understanding": {"subject": "dog"}}, computed)
    stored = clip_signals.read_signals(merged)["audio_quality"]
    assert stored["noisy"] is True
    assert set(stored) >= {"wind_score", "clipping", "noise_floor_db", "noisy", "evidence"}
    assert merged["understanding"] == {"subject": "dog"}
