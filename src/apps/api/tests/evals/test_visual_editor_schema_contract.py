"""Replay-only contracts for editor fields shared with agent output schemas."""

import pytest

from app.agents._schemas.media_overlay import coerce_media_overlays
from app.agents._schemas.visual_block import validate_visual_blocks


@pytest.mark.parametrize("lane", ["overlay", "block"])
@pytest.mark.parametrize("authored", [False, True])
def test_visual_editor_style_survives_lane_validation(lane, authored):
    payload = {
        "id": "media-1",
        "kind": "image" if lane == "overlay" else "media",
        "src_gcs_path": "users/test/media.png",
        "start_s": 0,
        "end_s": 2,
    }
    if lane == "block":
        payload.update(asset_id="asset-1", media_kind="image")
    style = {
        "version": 1,
        "rotation_deg": 35,
        "fit_mode": "contain",
        "zoom": 1.5,
        "animation": {
            "version": 1,
            "entrance": "pop",
            "exit": "fade",
            "loop": "float",
            "speed": 1.25,
        },
    }
    if authored:
        payload["editor_style"] = style
    if lane == "overlay":
        dropped = []
        result = coerce_media_overlays([payload], dropped_indices=dropped)
        assert result is not None and not dropped
        serialized = result[0].model_dump()
    else:
        serialized = validate_visual_blocks([payload], duration_s=3)[0]
    assert serialized.get("editor_style") == (style if authored else None)
    if lane == "block" and not authored:
        assert "editor_style" not in serialized
    assert serialized["src_gcs_path"] == payload["src_gcs_path"]
    assert (serialized["start_s"], serialized["end_s"]) == (0, 2)
