from __future__ import annotations

import math
import types
from pathlib import Path

import pytest

from app.pipeline.masonry_montage import (
    MASONRY_MAX_DURATION_S,
    _layout_for_config,
    _preset_config,
    _rotated_bounds,
    _rotation_for_config,
    _rotation_for_empty_pocket,
    assemble_masonry_montage,
    build_masonry_command,
    build_masonry_text_burn_command,
    build_masonry_tiles,
    clamp_masonry_duration,
    masonry_board_width_for_preset,
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


def _candidate_envelope(candidate):  # noqa: ANN001
    motion = candidate["masonry_motion"]
    return tuple(
        motion[key]
        for key in (
            "pocket_left_px",
            "pocket_top_px",
            "pocket_right_px",
            "pocket_bottom_px",
        )
    )


def _overlap_area(a, b):  # noqa: ANN001
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def test_masonry_text_placement_candidates_choose_stable_board_whitespace() -> None:
    candidates = masonry_text_placement_candidates(duration_s=8.0)

    assert [
        (item["x_frac"], item["y_frac"], item["max_width_frac"], item["rotation_deg"])
        for item in candidates
    ] == [
        (0.5861, 0.6625, 0.2266, 0.0),
        (0.5, 0.9359, 0.3944, 0.0),
    ]
    assert all(candidate["source"] == "masonry_whitespace" for candidate in candidates)
    assert candidates[0]["masonry_motion"]["mode"] == "masonry_pan_x"
    assert [candidate["masonry_motion"]["layer_origin_px"] for candidate in candidates] == [
        0.0,
        505.5,
    ]
    midpoint_scroll = candidates[0]["masonry_motion"]["pan_px"] * 2 / 8
    for candidate in candidates:
        left, _top, right, _bottom = _candidate_envelope(candidate)
        visible = max(0, min(right - midpoint_scroll, 1044) - max(left - midpoint_scroll, 36))
        assert visible / (right - left) >= 0.98


def test_masonry_candidate_envelopes_never_overlap_visible_tiles_during_reveal() -> None:
    duration = 8.0
    candidates = masonry_text_placement_candidates(duration_s=duration)
    config = _preset_config("masonry")
    layout = _layout_for_config(config)
    pan_px = candidates[0]["masonry_motion"]["pan_px"]

    for sample in range(7):
        time_s = 4.0 * sample / 6
        scroll = pan_px * min(time_s, duration) / duration
        tile_rects = [
            (
                x - config.placement_margin_px - scroll,
                y - config.placement_margin_px,
                x + width + config.placement_margin_px - scroll,
                y + height + config.placement_margin_px,
            )
            for x, y, width, height in layout
        ]
        for candidate in candidates:
            left, top, right, bottom = _candidate_envelope(candidate)
            envelope = (left - scroll, top, right - scroll, bottom)
            assert all(_overlap_area(envelope, tile) == 0 for tile in tile_rects)


@pytest.mark.parametrize(
    ("preset", "anchor_time"),
    [("masonry", 6.0), ("polaroid_wall", 4.0)],
)
def test_anchored_candidate_envelopes_never_overlap_rotated_tiles_during_reveal(
    preset: str,
    anchor_time: float,
) -> None:
    duration = 8.0
    candidates = masonry_text_placement_candidates(
        duration_s=duration,
        anchor_time_s=anchor_time,
        max_candidates=8,
        preset=preset,
    )
    assert candidates
    config = _preset_config(preset)
    layout = _layout_for_config(config)
    pan_px = candidates[0]["masonry_motion"]["pan_px"]

    for sample in range(7):
        time_s = 4.0 + 4.0 * sample / 6
        scroll = pan_px * min(time_s, duration) / duration
        tile_rects = []
        for index, (x, y, width, height) in enumerate(layout):
            rotated_width, rotated_height = _rotated_bounds(
                width,
                height,
                _rotation_for_config(config, index),
            )
            left = x - (rotated_width - width) / 2 - config.placement_margin_px
            top = y - (rotated_height - height) / 2 - config.placement_margin_px
            tile_rects.append(
                (
                    left - scroll,
                    top,
                    left + rotated_width + config.placement_margin_px * 2 - scroll,
                    top + rotated_height + config.placement_margin_px * 2,
                )
            )
        for candidate in candidates:
            left, top, right, bottom = _candidate_envelope(candidate)
            envelope = (left - scroll, top, right - scroll, bottom)
            assert all(_overlap_area(envelope, tile) == 0 for tile in tile_rects)


def test_masonry_leading_candidate_envelopes_are_pairwise_non_overlapping() -> None:
    candidates = masonry_text_placement_candidates(duration_s=8.0)
    envelopes = [_candidate_envelope(candidate) for candidate in candidates]

    for index, envelope in enumerate(envelopes):
        assert all(_overlap_area(envelope, other) == 0 for other in envelopes[index + 1 :])


def test_masonry_candidate_count_and_short_duration_are_deterministic() -> None:
    one = masonry_text_placement_candidates(duration_s=0.25, max_candidates=1)
    again = masonry_text_placement_candidates(duration_s=0.25, max_candidates=1)

    assert one == again
    assert len(one) == 1
    assert one[0]["masonry_motion"]["duration_s"] == 0.25


def test_masonry_candidates_include_pockets_revealed_later_by_board_pan() -> None:
    candidates = masonry_text_placement_candidates(
        duration_s=8.0,
        anchor_time_s=6.0,
        max_candidates=8,
    )

    late = [
        candidate
        for candidate in candidates
        if candidate["masonry_motion"].get("layer_origin_px", 0) > 0
    ]
    assert late
    assert [
        (
            candidate["x_frac"],
            candidate["y_frac"],
            candidate["max_width_frac"],
            candidate["rotation_deg"],
            candidate["masonry_motion"]["layer_origin_px"],
        )
        for candidate in late
    ] == [
        (0.5, 0.8021, 0.5479, 90.0, 854.0),
        (0.6505, 0.8641, 0.36, 90.0, 932.0),
    ]
    assert all(0.0 < candidate["x_frac"] < 1.0 for candidate in late)
    assert any(
        candidate["masonry_motion"]["layer_origin_px"]
        + candidate["x_frac"] * candidate["masonry_motion"]["frame_width_px"]
        > candidate["masonry_motion"]["frame_width_px"]
        for candidate in late
    )
    anchor_scroll = late[0]["masonry_motion"]["pan_px"] * 6 / 8
    for candidate in late:
        left, _top, right, _bottom = _candidate_envelope(candidate)
        assert left - anchor_scroll >= 36
        assert right - anchor_scroll <= 1044


def test_masonry_returns_no_candidate_when_layout_has_no_safe_pocket(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.masonry_montage._layout_for_config",
        lambda _config: [(0, 0, 1080, 1920)],
    )

    assert masonry_text_placement_candidates(duration_s=8.0) == []


def test_rotation_requires_a_qualifying_empty_vertical_pocket(monkeypatch) -> None:
    assert _rotation_for_empty_pocket(width=240, height=500) == 90.0
    assert _rotation_for_empty_pocket(width=400, height=500) == 0.0
    monkeypatch.setattr(
        "app.pipeline.masonry_montage._layout_for_config",
        lambda _config: [(0, 0, 720, 1920)],
    )

    candidates = masonry_text_placement_candidates(duration_s=8.0, max_candidates=1)

    assert candidates
    assert candidates[0]["rotation_deg"] == 90.0


def test_polaroid_text_placement_candidates_use_polaroid_layout() -> None:
    candidates = masonry_text_placement_candidates(duration_s=8.0, preset="polaroid_wall")

    assert candidates
    assert candidates[0]["source"] == "polaroid_wall_whitespace"
    assert 0.0 < candidates[0]["x_frac"] < 1.0
    assert 0.0 < candidates[0]["y_frac"] < 1.0
    assert 0.2 <= candidates[0]["max_width_frac"] <= 0.9
    assert candidates[0]["masonry_motion"]["board_width_px"] == masonry_board_width_for_preset(
        "polaroid_wall"
    )
    assert candidates[0]["masonry_motion"]["board_width_px"] > masonry_board_width_for_preset(
        "masonry"
    )

    config = _preset_config("polaroid_wall")
    tile_rects = []
    for index, (x, y, width, height) in enumerate(_layout_for_config(config)):
        rotated_width, rotated_height = _rotated_bounds(
            width,
            height,
            _rotation_for_config(config, index),
        )
        left = x - (rotated_width - width) / 2 - config.placement_margin_px
        top = y - (rotated_height - height) / 2 - config.placement_margin_px
        tile_rects.append(
            (
                left,
                top,
                left + rotated_width + config.placement_margin_px * 2,
                top + rotated_height + config.placement_margin_px * 2,
            )
        )
    for candidate in candidates:
        assert all(_overlap_area(_candidate_envelope(candidate), tile) == 0 for tile in tile_rects)


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


def test_build_masonry_text_burn_command_pans_text_with_board() -> None:
    cmd = build_masonry_text_burn_command(
        input_path="/tmp/base.mp4",
        sequences=[
            {
                "pattern": "/tmp/text%04d.png",
                "first_frame": "/tmp/text0000.png",
                "n_frames": 4,
                "fps": 30,
                "start_s": 0.0,
                "end_s": 3.0,
                "is_animated": True,
            }
        ],
        output_path="/tmp/out.mp4",
        duration_s=8.0,
        board_width=1600,
    )
    graph = cmd[cmd.index("-filter_complex") + 1]

    assert "overlay=x=-min(t\\,8.000)/8.000*520:y=0:eval=frame" in graph
    assert "setpts=PTS+0.0000/TB" in graph
    assert "-preset" in cmd
    assert cmd[cmd.index("-preset") + 1] == "fast"


def test_build_masonry_text_burn_command_offsets_late_text_layer_on_board() -> None:
    cmd = build_masonry_text_burn_command(
        input_path="/tmp/base.mp4",
        sequences=[
            {
                "pattern": "/tmp/text%04d.png",
                "first_frame": "/tmp/text0000.png",
                "n_frames": 1,
                "fps": 30,
                "start_s": 2.0,
                "end_s": 5.0,
                "is_animated": False,
                "layer_origin_x_px": 600,
            }
        ],
        output_path="/tmp/out.mp4",
        duration_s=8.0,
        board_width=2012,
    )
    graph = cmd[cmd.index("-filter_complex") + 1]

    assert "overlay=x=600-min(t\\,8.000)/8.000*932:y=0:eval=frame" in graph
