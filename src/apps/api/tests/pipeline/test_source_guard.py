"""Heavy-source downscale guard (2026-07-21 OOM incident, job e8173a25).

Pins the guard's scope contract:
  - SDR sources with short edge > threshold trigger; 1080p/1440p do not.
  - HDR sources NEVER trigger (the pre-tonemap pass owns those — an 8-bit
    re-encode here would destroy its input).
  - Still images NEVER trigger (image_clip owns image rendering).
  - Kill switch SOURCE_DOWNSCALE_GUARD_ENABLED=false → structural no-op.
  - The guard's own ffmpeg pass caps decoder threads (-threads BEFORE -i) —
    an unbounded-memory pre-pass would defeat the guard's purpose.
  - Best-effort: a failed conversion keeps the original path in place.

All subprocess calls are mocked — no ffmpeg needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.pipeline.source_guard import (
    build_downscale_cmd,
    downscale_oversized_sources,
    needs_downscale,
)


def _probe(width: int, height: int, color_trc: str = "bt709") -> SimpleNamespace:
    return SimpleNamespace(width=width, height=height, color_trc=color_trc)


# ── trigger decision ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (3840, 2160, True),  # landscape 4K
        (2160, 3840, True),  # portrait 4K
        (2160, 2160, True),  # square 4K-class
        (1080, 1920, False),  # native output
        (1920, 1080, False),  # 1080p landscape
        (1440, 2560, False),  # QHD portrait — short edge 1440
        (2560, 1440, False),  # QHD landscape
        (0, 0, False),  # broken probe dims
    ],
)
def test_needs_downscale_short_edge_rule(width: int, height: int, expected: bool) -> None:
    assert needs_downscale(_probe(width, height)) is expected


@pytest.mark.parametrize("trc", ["arib-std-b67", "smpte2084"])
def test_hdr_sources_never_trigger(trc: str) -> None:
    assert needs_downscale(_probe(3840, 2160, color_trc=trc)) is False


def test_kill_switch_disables_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "source_downscale_guard_enabled", False)
    assert needs_downscale(_probe(3840, 2160)) is False


# ── command shape ─────────────────────────────────────────────────────────────


def test_cmd_caps_decoder_threads_before_input() -> None:
    cmd = build_downscale_cmd("/tmp/in.mp4", "/tmp/out.mp4")
    i_idx = cmd.index("-i")
    threads_positions = [k for k, arg in enumerate(cmd) if arg == "-threads"]
    # One decoder-side cap (before -i) and one encoder-side cap (after -i).
    assert len(threads_positions) == 2
    assert threads_positions[0] < i_idx < threads_positions[1]
    assert cmd[threads_positions[0] + 1] == str(settings.source_downscale_ffmpeg_threads)


def test_cmd_never_upscales_and_keeps_quality_budget() -> None:
    cmd = build_downscale_cmd("/tmp/in.mp4", "/tmp/out.mp4")
    vf = cmd[cmd.index("-vf") + 1]
    # min(1, cover) forbids upscaling; iw/ih are post-autorotate so rotated
    # phone footage scales by display dims.
    assert f"min(1,max({settings.output_width}/iw,{settings.output_height}/ih))" in vf
    assert "lanczos" in vf
    # crf 16 / preset fast — the _pretonemap_hdr_clips gradient budget, NOT
    # ultrafast (this generation feeds the banding-sensitive final encode).
    assert cmd[cmd.index("-crf") + 1] == "16"
    assert cmd[cmd.index("-preset") + 1] == "fast"
    # Original audio must survive for original-audio variants.
    assert cmd[cmd.index("-c:a") + 1] == "copy"


# ── in-place conversion loop ──────────────────────────────────────────────────


def _run_guard(paths, probe_map, tmp_path, run_mock, probe_mock):
    with (
        patch("subprocess.run", run_mock),
        patch("app.pipeline.probe.probe_video", probe_mock),
        patch("app.services.pipeline_trace.record_pipeline_event"),
    ):
        return downscale_oversized_sources(paths, probe_map, str(tmp_path), job_id="test-job")


def test_oversized_clip_swapped_in_place_and_original_deleted(tmp_path) -> None:
    src = tmp_path / "big.mp4"
    src.write_bytes(b"x")
    paths = [str(src)]
    probe_map = {str(src): _probe(2160, 3840)}
    new_probe = _probe(1080, 1920)

    def _fake_run(cmd, **kwargs):
        # The output path is the last arg — materialize it like ffmpeg would.
        with open(cmd[-1], "wb") as fh:
            fh.write(b"y")
        return MagicMock(returncode=0)

    converted = _run_guard(paths, probe_map, tmp_path, _fake_run, MagicMock(return_value=new_probe))

    assert converted == 1
    assert paths[0] != str(src)
    assert paths[0].startswith(str(tmp_path))
    assert probe_map[paths[0]] is new_probe
    # The original's probe entry must be GONE — _available_footage_s sums
    # probe_map.values(), so a stale entry double-counts this clip's duration.
    assert str(src) not in probe_map
    assert list(probe_map) == [paths[0]]
    assert not src.exists()  # tmpfs is RAM — the original must be freed


def test_normal_clip_untouched(tmp_path) -> None:
    src = tmp_path / "normal.mp4"
    src.write_bytes(b"x")
    paths = [str(src)]
    probe_map = {str(src): _probe(1080, 1920)}
    run_mock = MagicMock()

    converted = _run_guard(paths, probe_map, tmp_path, run_mock, MagicMock())

    assert converted == 0
    assert paths == [str(src)]
    run_mock.assert_not_called()
    assert src.exists()


def test_image_file_skipped_even_with_oversized_dims(tmp_path) -> None:
    src = tmp_path / "photo.jpg"
    src.write_bytes(b"x")
    paths = [str(src)]
    # Still images carry a synthetic probe with real pixel dims (12MP photo).
    probe_map = {str(src): _probe(4032, 3024)}
    run_mock = MagicMock()

    converted = _run_guard(paths, probe_map, tmp_path, run_mock, MagicMock())

    assert converted == 0
    assert paths == [str(src)]
    run_mock.assert_not_called()


def test_failed_conversion_keeps_original(tmp_path) -> None:
    src = tmp_path / "big.mp4"
    src.write_bytes(b"x")
    paths = [str(src)]
    probe_map = {str(src): _probe(3840, 2160)}
    run_mock = MagicMock(side_effect=RuntimeError("boom"))

    converted = _run_guard(paths, probe_map, tmp_path, run_mock, MagicMock())

    assert converted == 0
    assert paths == [str(src)]
    assert src.exists()
    assert probe_map == {str(src): probe_map[str(src)]}
