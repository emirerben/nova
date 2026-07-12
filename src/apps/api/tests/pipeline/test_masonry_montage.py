from __future__ import annotations

import math
import types
from pathlib import Path

from app.pipeline.masonry_montage import (
    MASONRY_MAX_DURATION_S,
    assemble_masonry_montage,
    build_masonry_command,
    build_masonry_tiles,
    clamp_masonry_duration,
    masonry_text_placement_candidates,
)


def _rectangles_overlap(a, b) -> bool:  # noqa: ANN001
    return not (
        a.x + a.width <= b.x
        or b.x + b.width <= a.x
        or a.y + a.height <= b.y
        or b.y + b.height <= a.y
    )


def _visual_bounds(tile):  # noqa: ANN001
    radians = math.radians(abs(tile.rotation_deg))
    rotated_width = math.ceil(tile.width * math.cos(radians) + tile.height * math.sin(radians))
    rotated_height = math.ceil(tile.width * math.sin(radians) + tile.height * math.cos(radians))
    return types.SimpleNamespace(
        x=tile.x - round((rotated_width - tile.width) / 2),
        y=tile.y - round((rotated_height - tile.height) / 2),
        width=rotated_width,
        height=rotated_height,
    )


def test_clamp_masonry_duration_caps_to_reference_window() -> None:
    assert clamp_masonry_duration(99.0) == MASONRY_MAX_DURATION_S
    assert clamp_masonry_duration(4.25) == 4.25


def test_masonry_text_placement_candidates_choose_whitespace() -> None:
    candidates = masonry_text_placement_candidates(duration_s=8.0)

    assert candidates
    assert candidates[0]["source"] == "masonry_whitespace"
    assert candidates[0]["x_frac"] > 0.75
    assert candidates[0]["y_frac"] > 0.85
    assert 0.2 <= candidates[0]["max_width_frac"] <= 0.32


def test_polaroid_text_placement_candidates_use_polaroid_layout() -> None:
    candidates = masonry_text_placement_candidates(duration_s=8.0, preset="polaroid_wall")

    assert candidates
    assert candidates[0]["source"] == "polaroid_wall_whitespace"
    assert 0.0 < candidates[0]["x_frac"] < 1.0
    assert 0.0 < candidates[0]["y_frac"] < 1.0
    assert 0.2 <= candidates[0]["max_width_frac"] <= 0.9


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


def test_build_polaroid_wall_tiles_write_frames_and_mark_featured_cards(tmp_path) -> None:
    steps = [types.SimpleNamespace(clip_id="clip_0"), types.SimpleNamespace(clip_id="clip_1")]
    tiles = build_masonry_tiles(
        steps=steps,
        clip_id_to_local={"clip_0": "/tmp/a.mp4", "clip_1": "/tmp/b.jpg"},
        mask_dir=str(tmp_path / "masks"),
        max_tiles=18,
        preset="polaroid_wall",
    )

    assert len(tiles) == 18
    assert all(tile.frame_path for tile in tiles)
    assert all(Path(tile.frame_path or "").exists() for tile in tiles)
    assert all(tile.content_width < tile.width for tile in tiles)
    assert all(tile.content_height < tile.height for tile in tiles)
    assert any(abs(tile.rotation_deg) > 0.1 for tile in tiles)
    assert tiles[0].width != 270
    assert tiles[1].height != 250
    assert tiles[3].z_index > tiles[0].z_index
    assert tiles[6].z_index > tiles[3].z_index
    assert tiles[11].z_index > tiles[0].z_index
    assert tiles[15].z_index > tiles[0].z_index
    assert tiles[3].width >= 600
    assert tiles[6].height >= 720
    assert tiles[11].width >= 540
    assert tiles[15].width >= 560
    for idx, tile in enumerate(tiles):
        for other in tiles[idx + 1 :]:
            assert not _rectangles_overlap(tile, other), (tile, other)
            assert not _rectangles_overlap(_visual_bounds(tile), _visual_bounds(other)), (
                tile,
                other,
            )


def test_build_polaroid_wall_command_uses_frames_hero_order_and_fast_preset(tmp_path) -> None:
    steps = [types.SimpleNamespace(clip_id="clip_0"), types.SimpleNamespace(clip_id="clip_1")]
    tiles = build_masonry_tiles(
        steps=steps,
        clip_id_to_local={"clip_0": "/tmp/a.mp4", "clip_1": "/tmp/b.jpg"},
        mask_dir=str(tmp_path / "masks"),
        max_tiles=8,
        preset="polaroid_wall",
    )
    cmd = build_masonry_command(
        tiles=tiles,
        output_path="/tmp/out.mp4",
        duration_s=99.0,
        board_width=1800,
        audio_source_path="/tmp/audio.mp4",
    )
    filter_graph = cmd[cmd.index("-filter_complex") + 1]
    hero = tiles[6]

    assert any("masonry_frames" in arg for arg in cmd)
    assert f"scale={hero.content_width}:{hero.content_height}" in filter_graph
    assert "rotate=" in filter_graph
    assert f"[frame6][tile6]overlay=x={hero.content_x}:y={hero.content_y}" in filter_graph
    assert filter_graph.rfind("[card3rot]overlay") > filter_graph.rfind("[card7rot]overlay")
    assert filter_graph.rfind("[card6rot]overlay") > filter_graph.rfind("[card3rot]overlay")
    assert filter_graph.endswith("[outv]")
    assert "/tmp/audio.mp4" in cmd
    assert f"{MASONRY_MAX_DURATION_S:.3f}" in cmd
    assert cmd[cmd.index("-preset") + 1] == "fast"


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
