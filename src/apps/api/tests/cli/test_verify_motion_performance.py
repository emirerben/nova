from __future__ import annotations

import json

import pytest

from app.cli import verify_motion_performance as verifier
from app.pipeline.motion_scene import validate_motion_instances


def test_maximum_complexity_fixture_is_exactly_240_frames() -> None:
    scene = verifier._maximum_complexity_scene()
    assert scene["end_frame_exclusive"] - scene["start_frame"] == 240
    assert scene["params"]["icon_count"] == 5
    assert scene["params"]["morph_amplitude"] == 1
    assert len(scene["params"]["headline"]) == 48
    assert len(scene["params"]["subtitle"]) == 72
    assert scene["params"]["typography_scale"] == 2
    assert validate_motion_instances([scene], duration_frames=240) == [scene]


def test_media_fixture_exercises_eight_blocks_and_sixty_four_unique_assets() -> None:
    scenes = verifier._maximum_media_scenes()
    asset_ids = {asset["asset_id"] for scene in scenes for asset in scene["params"]["assets"]}
    assert len(scenes) == 8
    assert all(scene["end_frame_exclusive"] == 36 for scene in scenes)
    assert len(asset_ids) == 64
    assert validate_motion_instances(scenes, duration_frames=240) == scenes


def test_benchmark_asset_is_deterministic_normalized_png() -> None:
    first = verifier._normalized_benchmark_png()
    assert first == verifier._normalized_benchmark_png()
    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = verifier.struct.unpack(">II", first[16:24])
    assert (width, height) == (2048, 2048)


@pytest.mark.parametrize(
    ("elapsed", "peak", "passes"),
    [(179.0, 2_000_000_000, True), (181.0, 2_000_000_000, False), (10.0, 3_000_000_000, False)],
)
def test_motion_performance_cli_enforces_time_and_memory_limits(
    monkeypatch,
    tmp_path,
    elapsed: float,
    peak: int,
    passes: bool,
) -> None:
    ticks = iter((10.0, 10.0 + elapsed, 20.0, 20.0 + elapsed))
    monkeypatch.setattr(verifier.time, "perf_counter", lambda: next(ticks))
    peaks = iter((peak - 1, peak))
    monkeypatch.setattr(verifier, "_peak_child_bytes", lambda: next(peaks))
    monkeypatch.setattr(verifier, "_write_benchmark_assets", lambda *_args: {})
    monkeypatch.setattr(
        verifier,
        "_render_sequence",
        lambda scenes, *_args, **_kwargs: (
            "frames",
            [
                {
                    "start_frame": 0,
                    "end_frame_exclusive": 36 if len(scenes) == 8 else 240,
                }
            ],
            36 if len(scenes) == 8 else 240,
        ),
    )
    monkeypatch.setattr(
        verifier.sys,
        "argv",
        ["verify_motion_performance", "--out", str(tmp_path)],
    )

    if passes:
        verifier.main()
    else:
        with pytest.raises(SystemExit, match="1"):
            verifier.main()

    report = json.loads((tmp_path / "performance.json").read_text())
    assert report["ok"] is passes
    assert report["frame_count"] == 276
    assert [case["name"] for case in report["cases"]] == [
        "evolving_max_content",
        "film_strip_max_resources",
    ]
    assert report["elapsed_seconds"] == elapsed
    assert report["peak_worker_child_bytes"] == peak
