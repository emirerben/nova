from __future__ import annotations

import copy
import subprocess

import numpy as np
import pytest

from app.pipeline.guided_story import GuidedStoryError, _mux_guided_source_audio


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, timeout=30).stdout


@pytest.fixture(scope="module")
def audio_fixture(tmp_path_factory):
    directory = tmp_path_factory.mktemp("reference-audio")
    paths = {}
    for name, frequency in (("a", 440), ("b", 880), ("silent", None)):
        path = directory / f"{name}.mp4"
        args = ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=30"]
        if frequency:
            args += [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate=48000:duration=3",
            ]
        args += ["-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)]
        run(*args)
        paths[name] = str(path)
    assembled = directory / "assembled.mp4"
    run(
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=64x64:r=30",
        "-t",
        "4.5",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(assembled),
    )
    plan = {
        "compiler_version": 6,
        "resolved_duration_s": 4.5,
        "editor_audio_level": 0.4,
        "montage_audio": {"preserve_source_audio": True},
        "story_timeline": [
            # A silent branch before audible branches catches filter-label gaps.
            moment("silent", 0, 0.2, 0),
            moment("a", 0.5, 2.5, 0),
            moment("b", 0.25, 2.25, 1.5),
            moment("silent", 0, 1, 3.5),
            moment("a", 1, 1.5, 3.75),
        ],
    }
    return str(assembled), paths, plan


def moment(media, start, end, output_start):
    return {
        "media_id": media,
        "kind": "video",
        "source_start_s": start,
        "source_end_s": end,
        "output_start_s": output_start,
        "output_end_s": output_start + end - start,
        "duration_s": end - start,
    }


def decode(path):
    return np.frombuffer(
        run(
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "f32le",
            "-",
        ),
        dtype="<f4",
    )


def amplitude(samples, start, end, frequency):
    window = samples[round(start * 48000) : round(end * 48000)].astype(np.float64)
    phase = np.arange(len(window)) * (2 * np.pi * frequency / 48000)
    return 2 * abs(np.dot(window, np.exp(-1j * phase))) / len(window)


def test_source_overlap_pcm_preserves_both_tones_trim_gain_and_silent_tail(audio_fixture, tmp_path):
    assembled, paths, plan = audio_fixture
    output = _mux_guided_source_audio(assembled, plan, paths, str(tmp_path / "mixed.mp4"))
    samples = decode(output)
    assert len(samples) / 48000 == pytest.approx(4.5, abs=0.035)
    expected = {
        frequency: amplitude(decode(paths[name]), 0.5, 0.9, frequency) * 0.4
        for name, frequency in (("a", 440), ("b", 880))
    }
    for start, end, active in [
        (0.4, 0.8, {440}),
        (1.65, 1.85, {440, 880}),
        (2.2, 2.6, {880}),
        (3.85, 4.05, {440}),
        (4.3, 4.45, set()),
    ]:
        for frequency in (440, 880):
            assert amplitude(samples, start, end, frequency) == pytest.approx(
                expected[frequency] if frequency in active else 0, abs=0.004
            ), (start, end, frequency)
    # The assembled picture survives a silent tail, rather than ending at the last tone.
    video_duration = float(
        run(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=duration",
            "-of",
            "csv=p=0",
            output,
        )
    )
    assert video_duration == pytest.approx(4.5, abs=0.035)


def test_zero_original_level_stays_silent(audio_fixture, tmp_path):
    assembled, paths, source_plan = audio_fixture
    plan = {**source_plan, "editor_audio_level": 0}
    samples = decode(_mux_guided_source_audio(assembled, plan, paths, str(tmp_path / "zero.mp4")))
    assert float(np.max(np.abs(samples))) < 0.0001


def test_all_sources_without_audio_emit_full_duration_silence(audio_fixture, tmp_path):
    assembled, paths, source_plan = audio_fixture
    plan = {**source_plan, "story_timeline": [moment("silent", 0.5, 2.5, 0)]}
    samples = decode(_mux_guided_source_audio(assembled, plan, paths, str(tmp_path / "silent.mp4")))
    assert len(samples) / 48000 == pytest.approx(4.5, abs=0.035)
    assert float(np.max(np.abs(samples))) < 0.0001


def test_disabled_source_policy_leaves_assembled_untouched(audio_fixture, tmp_path):
    assembled, _, source_plan = audio_fixture
    plan = {**source_plan, "montage_audio": {"preserve_source_audio": False}}
    assert _mux_guided_source_audio(assembled, plan, {}, str(tmp_path / "unused.mp4")) == assembled


def test_unreadable_approved_source_fails_closed(audio_fixture, tmp_path):
    assembled, paths, plan = audio_fixture
    with pytest.raises(GuidedStoryError, match="could not be inspected"):
        _mux_guided_source_audio(
            assembled,
            plan,
            {**paths, "a": str(tmp_path / "missing.mp4")},
            str(tmp_path / "bad.mp4"),
        )


def test_rate_adjusted_source_has_expected_window(audio_fixture, tmp_path):
    assembled, paths, source_plan = audio_fixture
    plan = copy.deepcopy(source_plan)
    plan["story_timeline"] = [
        {**moment("a", 0.5, 1, 0.25), "playback_rate": 0.25, "duration_s": 2, "output_end_s": 2.25}
    ]
    samples = decode(_mux_guided_source_audio(assembled, plan, paths, str(tmp_path / "slow.mp4")))
    assert amplitude(samples, 0.6, 1, 440) == pytest.approx(0.125 * 0.4, abs=0.005)
    assert amplitude(samples, 2.6, 3, 440) < 0.001
