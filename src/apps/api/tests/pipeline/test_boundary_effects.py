from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.pipeline.boundary_effects import (
    BoundaryEffectError,
    build_boundary_effects_command,
)


def _filter_graph(cmd: list[str]) -> str:
    return cmd[cmd.index("-filter_complex") + 1]


def test_horizontal_motion_blur_gates_expensive_filters_and_preserves_output_policy(
    tmp_path,
) -> None:
    cmd = build_boundary_effects_command(
        "target.mp4",
        [
            {
                "effect": "horizontal_motion_blur",
                "at_s": 5.8,
                "duration_s": 0.42,
                "blur_sigma": 44.0,
                "intensity": 1.0,
            }
        ],
        str(tmp_path / "out.mp4"),
    )
    graph = _filter_graph(cmd)
    activation = "enable='between(t\\,5.800\\,6.220)'"

    assert cmd.count("-i") == 1
    assert "gblur=sigma=44.000:sigmaV=1:steps=6" in graph
    assert graph.count(activation) == 2
    assert "between(T\\,5.800\\,6.220)" in graph
    assert cmd[cmd.index("-map") + 1] == "[boundary_out]"
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert "0:a:0?" in cmd
    assert cmd[cmd.index("-preset") + 1] == "fast"
    assert "ultrafast" not in cmd


def test_multiple_windows_share_one_normalized_activation_union(tmp_path) -> None:
    cmd = build_boundary_effects_command(
        "target.mp4",
        [
            {
                "effect": "horizontal_motion_blur",
                "at_s": -2.0,
                "duration_s": 0.01,
                "blur_sigma": 20.0,
                "intensity": -1.0,
            },
            {
                "effect": "horizontal_motion_blur",
                "at_s": 0.05,
                "duration_s": 0.3,
                "blur_sigma": 72.0,
                "intensity": 2.0,
            },
        ],
        str(tmp_path / "out.mp4"),
    )
    graph = _filter_graph(cmd)
    activation = "enable='max(between(t\\,0.000\\,0.120)\\,between(t\\,0.050\\,0.350))'"

    assert graph.count(activation) == 2
    assert graph.count("split=2") == 1
    assert graph.count("blend=all_expr") == 1
    assert "gblur=sigma=72.000" in graph
    assert "sin(PI*(T-0.000)/0.120)*0.000" in graph
    assert "sin(PI*(T-0.050)/0.300)*1.000" in graph
    assert "max(" in graph


def test_unsupported_and_non_positive_effects_are_skipped(tmp_path) -> None:
    cmd = build_boundary_effects_command(
        "target.mp4",
        [
            {"effect": "zoom", "at_s": 1.0, "duration_s": 0.5},
            {
                "effect": "horizontal_motion_blur",
                "at_s": 2.0,
                "duration_s": 0.0,
            },
            {
                "effect": "horizontal_motion_blur",
                "at_s": 3.0,
                "duration_s": 0.4,
            },
        ],
        str(tmp_path / "out.mp4"),
    )
    graph = _filter_graph(cmd)

    assert "between(t\\,3.000\\,3.400)" in graph
    assert "between(t\\,1.000" not in graph
    assert "between(t\\,2.000" not in graph


@pytest.mark.parametrize(
    "effects",
    [
        [],
        [{"effect": "zoom", "duration_s": 0.5}],
        [{"effect": "horizontal_motion_blur", "duration_s": 0.0}],
        [{"effect": "horizontal_motion_blur", "duration_s": -0.1}],
    ],
)
def test_no_valid_effects_raise(effects, tmp_path) -> None:
    with pytest.raises(BoundaryEffectError, match="no supported boundary effects"):
        build_boundary_effects_command(
            "target.mp4",
            effects,
            str(tmp_path / "out.mp4"),
        )


def test_max_event_budget_uses_balanced_expression_trees(tmp_path) -> None:
    cmd = build_boundary_effects_command(
        "target.mp4",
        [
            {
                "effect": "horizontal_motion_blur",
                "at_s": index * 0.1,
                "duration_s": 0.42,
            }
            for index in range(120)
        ],
        str(tmp_path / "out.mp4"),
    )
    graph = _filter_graph(cmd)
    activation = graph.split("enable='", 1)[1].split("'", 1)[0]

    assert activation.count("between(t") == 120
    assert activation.count("max(") == 119
    assert activation.count("max(max(max(max(max(max(max(max(") == 0


_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, capture_output=True, check=True, timeout=60)


def _decoded_hash(path: Path, stream: str) -> str:
    return subprocess.check_output(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            stream,
            "-f",
            "hash",
            "-hash",
            "sha256",
            "-",
        ],
        text=True,
        timeout=60,
    ).strip()


def _probe(path: Path) -> dict:
    return json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-show_entries",
                (
                    "stream=index,codec_type,codec_name,time_base,duration,"
                    "nb_read_frames,sample_rate,channels:format=duration"
                ),
                "-of",
                "json",
                str(path),
            ],
            text=True,
            timeout=60,
        )
    )


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_overlapping_timeline_gating_matches_legacy_frames_audio_and_stream_metadata(
    monkeypatch,
    tmp_path,
) -> None:
    from app.pipeline import reframe

    monkeypatch.setattr(reframe.settings, "output_width", 96)
    monkeypatch.setattr(reframe.settings, "output_height", 160)
    monkeypatch.setattr(reframe.settings, "output_fps", 15)

    source = tmp_path / "source.mp4"
    legacy = tmp_path / "legacy.mp4"
    optimized = tmp_path / "optimized.mp4"
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x160:rate=15:duration=1.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=44100:duration=1.2",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-y",
            str(source),
        ]
    )
    effects = [
        {
            "effect": "horizontal_motion_blur",
            "at_s": 0.4,
            "duration_s": 0.3,
            "blur_sigma": 22.0,
            "intensity": 0.8,
        },
        {
            "effect": "horizontal_motion_blur",
            "at_s": 0.55,
            "duration_s": 0.35,
            "blur_sigma": 30.0,
            "intensity": 0.6,
        },
    ]
    optimized_cmd = build_boundary_effects_command(str(source), effects, str(optimized))
    legacy_cmd = build_boundary_effects_command(str(source), effects, str(legacy))
    graph_index = legacy_cmd.index("-filter_complex") + 1
    legacy_cmd[graph_index] = re.sub(
        r":enable='[^']+'",
        "",
        legacy_cmd[graph_index],
    )

    _run(legacy_cmd)
    _run(optimized_cmd)

    assert _decoded_hash(legacy, "0:v:0") == _decoded_hash(optimized, "0:v:0")
    assert _decoded_hash(legacy, "0:a:0") == _decoded_hash(optimized, "0:a:0")
    assert _probe(legacy) == _probe(optimized)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_max_event_budget_executes_with_real_ffmpeg(monkeypatch, tmp_path) -> None:
    from app.pipeline import reframe

    monkeypatch.setattr(reframe.settings, "output_width", 96)
    monkeypatch.setattr(reframe.settings, "output_height", 160)
    monkeypatch.setattr(reframe.settings, "output_fps", 15)

    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x160:rate=15:duration=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=44100:duration=0.5",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(source),
        ]
    )
    effects = [
        {
            "effect": "horizontal_motion_blur",
            "at_s": index * 0.001,
            "duration_s": 0.42,
            "blur_sigma": 44.0,
            "intensity": 1.0,
        }
        for index in range(120)
    ]

    _run(build_boundary_effects_command(str(source), effects, str(output)))

    assert output.stat().st_size > 0
