from __future__ import annotations

from app.agents._schemas.visual_block import AudioPolicy, MontageBlock
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
