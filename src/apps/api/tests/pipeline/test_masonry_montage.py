from __future__ import annotations

import types
from pathlib import Path

from app.pipeline.masonry_montage import (
    MASONRY_MAX_DURATION_S,
    assemble_masonry_montage,
    build_masonry_command,
    build_masonry_tiles,
    clamp_masonry_duration,
)


def test_clamp_masonry_duration_caps_to_reference_window() -> None:
    assert clamp_masonry_duration(99.0) == MASONRY_MAX_DURATION_S
    assert clamp_masonry_duration(4.25) == 4.25


def test_build_masonry_tiles_cycles_uploaded_clips_and_writes_masks(tmp_path) -> None:
    steps = [types.SimpleNamespace(clip_id="clip_0"), types.SimpleNamespace(clip_id="clip_1")]
    tiles = build_masonry_tiles(
        steps=steps,
        clip_id_to_local={"clip_0": "/tmp/a.mp4", "clip_1": "/tmp/b.mp4"},
        mask_dir=str(tmp_path),
        max_tiles=5,
    )

    assert len(tiles) == 5
    assert [t.clip_id for t in tiles] == ["clip_0", "clip_1", "clip_0", "clip_1", "clip_0"]
    assert all((tmp_path / f"mask_{t.width}x{t.height}.png").exists() for t in tiles)


def test_build_masonry_command_loops_still_images_as_image_inputs(tmp_path) -> None:
    steps = [types.SimpleNamespace(clip_id="clip_0"), types.SimpleNamespace(clip_id="clip_1")]
    tiles = build_masonry_tiles(
        steps=steps,
        clip_id_to_local={"clip_0": "/tmp/a.jpg", "clip_1": "/tmp/b.mp4"},
        mask_dir=str(tmp_path),
        max_tiles=2,
    )
    cmd = build_masonry_command(
        tiles=tiles,
        output_path="/tmp/out.mp4",
        duration_s=8.0,
        board_width=1600,
    )

    image_idx = cmd.index("/tmp/a.jpg")
    video_idx = cmd.index("/tmp/b.mp4")
    assert cmd[image_idx - 5 : image_idx] == ["-loop", "1", "-t", "8.000", "-i"]
    assert cmd[video_idx - 5 : video_idx] == ["-stream_loop", "-1", "-t", "8.000", "-i"]
    filter_graph = cmd[cmd.index("-filter_complex") + 1]
    assert "format=rgba,scale=270:480" in filter_graph
    assert "setpts=PTS-STARTPTS,setsar=1,format=rgba" in filter_graph


def test_assemble_masonry_normalizes_heic_tiles_before_ffmpeg(tmp_path, monkeypatch) -> None:
    from PIL import Image

    source = tmp_path / "photo.heic"
    Image.new("RGB", (1537, 2048), color=(220, 40, 40)).save(source, format="PNG")
    output = tmp_path / "out.mp4"
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, *args, **kwargs):  # noqa: ANN001, ARG001
        captured["cmd"] = list(cmd)
        output.write_bytes(b"ok")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("app.pipeline.masonry_montage.subprocess.run", fake_run)

    assemble_masonry_montage(
        steps=[types.SimpleNamespace(clip_id="clip_0")],
        clip_id_to_local={"clip_0": str(source)},
        output_path=str(output),
        tmpdir=str(tmp_path),
        duration_s=2.0,
    )

    cmd = captured["cmd"]
    normalized_inputs = [
        arg for arg in cmd if "masonry_images" in arg and Path(arg).suffix == ".png"
    ]
    assert str(source) not in cmd
    assert normalized_inputs
    image_idx = cmd.index(normalized_inputs[0])
    assert cmd[image_idx - 5 : image_idx] == ["-loop", "1", "-t", "2.000", "-i"]


def test_build_masonry_command_uses_alpha_masks_audio_and_fast_preset(tmp_path) -> None:
    steps = [types.SimpleNamespace(clip_id="clip_0")]
    tiles = build_masonry_tiles(
        steps=steps,
        clip_id_to_local={"clip_0": "/tmp/a.mp4"},
        mask_dir=str(tmp_path),
        max_tiles=1,
    )
    cmd = build_masonry_command(
        tiles=tiles,
        output_path="/tmp/out.mp4",
        duration_s=99.0,
        board_width=1600,
        audio_source_path="/tmp/audio.mp4",
    )
    joined = " ".join(cmd)

    assert "alphamerge" in joined
    assert "overlay=x=" in joined
    assert "min(t\\," in joined
    assert "min(t," not in joined
    assert "/tmp/audio.mp4" in cmd
    assert "-map" in cmd
    assert "3:a:0?" in cmd
    assert "-preset" in cmd
    assert cmd[cmd.index("-preset") + 1] == "fast"
    assert f"{MASONRY_MAX_DURATION_S:.3f}" in cmd
