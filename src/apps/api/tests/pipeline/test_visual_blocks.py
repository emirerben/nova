from __future__ import annotations

import json
from pathlib import Path

from app.agents._schemas.visual_block import AudioPolicy, MediaBlock, MediaTransform, MontageBlock
from app.pipeline.visual_blocks import build_visual_block_composite_command


def _block() -> MontageBlock:
    shots = []
    for index in range(3):
        shots.append(
            {
                "asset_id": f"asset-{index}",
                "src_gcs_path": f"users/u/plan/p/pool/{index}.jpg",
                "kind": "image",
                "start_offset_s": float(index),
                "duration_s": 1.0,
            }
        )
    return MontageBlock(
        kind="montage",
        start_s=1.0,
        end_s=4.0,
        shots=shots,
        transition_in="fade",
        audio_policy={"base": "mute", "sfx": "continue"},
    )


def test_composite_command_places_blocks_below_future_text_and_mutes_base() -> None:
    cmd = build_visual_block_composite_command(
        "base.mp4", [_block()], ["replacement.mp4"], "out.mp4"
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "overlay=0:0" in graph
    assert "between(t,1.000000,4.000000)" in graph
    assert "volume=0" in graph
    assert cmd[cmd.index("-preset") + 1] == "fast"


def test_no_base_mute_maps_original_audio_without_filtering() -> None:
    block = _block().model_copy(update={"audio_policy": AudioPolicy()})
    cmd = build_visual_block_composite_command("base.mp4", [block], ["replacement.mp4"], "out.mp4")
    assert "0:a?" in cmd


def _media(
    *,
    media_kind: str = "image",
    display_mode: str = "fullscreen",
    start_s: float = 1.0,
    end_s: float = 3.0,
    z: int = 0,
    transform: dict | None = None,
    **kwargs,
) -> MediaBlock:
    return MediaBlock(
        kind="media",
        asset_id=f"asset-{z}",
        src_gcs_path=f"users/u/media-{z}.{'jpg' if media_kind == 'image' else 'mp4'}",
        media_kind=media_kind,
        display_mode=display_mode,
        start_s=start_s,
        end_s=end_s,
        z=z,
        transform=transform or {},
        source_duration_s=kwargs.pop("source_duration_s", 8.0) if media_kind == "video" else None,
        trim_start_s=kwargs.pop("trim_start_s", None),
        trim_end_s=kwargs.pop("trim_end_s", None),
        **kwargs,
    )


def test_media_fullscreen_contain_reveals_base_and_uses_half_open_window() -> None:
    block = _media(
        transform={"fit_mode": "contain", "focal_x": 0.2, "focal_y": 0.8, "zoom": 1.5},
        transition_in="fade",
        transition_out="fade",
    )
    cmd = build_visual_block_composite_command("base.mp4", [block], ["photo.jpg"], "out.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]

    assert "-loop" in cmd
    assert "scale=1620:2880:force_original_aspect_ratio=decrease" in graph
    assert "overlay=(main_w-overlay_w)*0.200000:(main_h-overlay_h)*0.800000" in graph
    assert "enable='gte(t,1.000000)*lt(t,3.000000)'" in graph
    assert "fade=t=in" in graph and "fade=t=out" in graph


def test_media_render_boundaries_round_to_output_frames() -> None:
    block = _media(start_s=1.11, end_s=1.24, audio_policy={"base": "mute", "sfx": "continue"})
    cmd = build_visual_block_composite_command("base.mp4", [block], ["photo.jpg"], "out.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]

    assert "enable='gte(t,1.100000)*lt(t,1.233333)'" in graph
    assert "volume=0:enable='gte(t,1.100000)*lt(t,1.233333)'" in graph


def test_media_cover_honors_focal_point_and_zoom() -> None:
    block = _media(transform=MediaTransform(fit_mode="cover", focal_x=0.2, focal_y=0.8, zoom=1.5))
    cmd = build_visual_block_composite_command("base.mp4", [block], ["photo.jpg"], "out.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]

    assert "scale=1620:2880:force_original_aspect_ratio=increase" in graph
    assert "x='(iw-ow)*0.200000'" in graph
    assert "y='(ih-oh)*0.800000'" in graph
    assert "overlay=0:0" in graph


def test_renderer_matches_shared_editor_geometry_fixture() -> None:
    fixture_path = Path(__file__).resolve().parents[5] / "tests/fixtures/media-geometry/v1.json"
    cases = json.loads(fixture_path.read_text())["cases"]
    for case in cases:
        block = _media(
            transform=MediaTransform(
                fit_mode=case["fit_mode"],
                focal_x=case["focal_x"],
                focal_y=case["focal_y"],
                zoom=case["zoom"],
            )
        )
        cmd = build_visual_block_composite_command("base.mp4", [block], ["photo.jpg"], "out.mp4")
        graph = cmd[cmd.index("-filter_complex") + 1]
        render = case["render"]
        assert (
            f"scale={render['target_width']}:{render['target_height']}:"
            f"force_original_aspect_ratio={render['aspect_rule']}" in graph
        ), case["name"]
        if case["fit_mode"] == "cover":
            assert f"x='(iw-ow)*{case['focal_x']:.6f}'" in graph, case["name"]
            assert f"y='(ih-oh)*{case['focal_y']:.6f}'" in graph, case["name"]
        else:
            assert f"(main_w-overlay_w)*{case['focal_x']:.6f}" in graph, case["name"]
            assert f"(main_h-overlay_h)*{case['focal_y']:.6f}" in graph, case["name"]


def test_media_video_trims_source_and_composites_overlay_geometry() -> None:
    block = _media(
        media_kind="video",
        display_mode="overlay",
        start_s=2.0,
        end_s=5.0,
        x_frac=0.25,
        y_frac=0.75,
        scale=0.4,
        trim_start_s=1.0,
        trim_end_s=4.0,
    )
    cmd = build_visual_block_composite_command("base.mp4", [block], ["clip.mp4"], "out.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]

    clip_input = cmd.index("clip.mp4")
    assert cmd[clip_input - 3 : clip_input + 1] == ["-ss", "1.000000", "-i", "clip.mp4"]
    assert "trim=duration=3.000000" in graph
    assert "scale=432:-2" in graph
    assert "overlay=(1080*0.250000-overlay_w/2):(1920*0.750000-overlay_h/2)" in graph
    assert "enable='gte(t,2.000000)*lt(t,5.000000)'" in graph


def test_media_cards_sort_by_z_while_legacy_cards_keep_input_order() -> None:
    low = _media(z=0, start_s=0.0, end_s=2.0)
    high = _media(z=2, start_s=0.0, end_s=2.0)
    cmd = build_visual_block_composite_command(
        "base.mp4", [high, low], ["high.jpg", "low.jpg"], "out.mp4"
    )
    graph = cmd[cmd.index("-filter_complex") + 1]

    # z=0 is the first media overlay and z=2 is composited later/on top.
    assert graph.index("[2:v]") < graph.index("[1:v]")
    assert graph.index("media0") < graph.index("media1")


def test_mixed_structured_and_media_pass_uses_unique_graph_labels() -> None:
    structured = _block().model_copy(update={"start_s": 0.0, "end_s": 2.0})
    low = _media(z=0, start_s=0.0, end_s=2.0)
    high = _media(z=2, start_s=0.0, end_s=2.0)
    cmd = build_visual_block_composite_command(
        "base.mp4",
        [structured, high, low],
        ["structured.mp4", "high.jpg", "low.jpg"],
        "out.mp4",
    )
    graph = cmd[cmd.index("-filter_complex") + 1]

    # The structured replacement keeps its legacy vb label while media gets
    # labels from the globally indexed, z-sorted composite pass.
    assert "[vb0]" in graph
    assert "[media1]" in graph and "[media2]" in graph
    assert "[media0]" not in graph
