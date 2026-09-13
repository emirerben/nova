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


@pytest.mark.parametrize("lane", ["overlay", "block"])
@pytest.mark.parametrize("rate", [None, 0.25, 1.0, 4.0])
def test_source_controls_round_trip_without_changing_placement(lane, rate):
    payload = {
        "id": "source-controls",
        "kind": "video" if lane == "overlay" else "media",
        "src_gcs_path": "users/test/media.mp4",
        "start_s": 0,
        "end_s": 2,
    }
    if lane == "block":
        payload.update(
            asset_id="asset-1",
            media_kind="video",
            source_duration_s=8,
            trim_start_s=0,
            trim_end_s=2,
        )
    crop = {"x": 0.1, "y": 0.2, "width": 0.7, "height": 0.6}
    if rate is not None:
        payload.update(source_crop=crop, playback_rate=rate)
    if lane == "overlay":
        dropped = []
        result = coerce_media_overlays([payload], dropped_indices=dropped)
        assert result is not None and not dropped
        serialized = result[0].model_dump(exclude_none=True)
    else:
        serialized = validate_visual_blocks([payload], duration_s=3)[0]
    assert serialized.get("playback_rate") == rate
    assert serialized.get("source_crop") == (crop if rate is not None else None)
    assert serialized["id"] == payload["id"]
    assert (serialized["start_s"], serialized["end_s"]) == (0, 2)
    if rate is None:
        assert "playback_rate" not in serialized
        assert "source_crop" not in serialized


@pytest.mark.parametrize("lane", ["overlay", "block"])
@pytest.mark.parametrize(
    "controls",
    [
        {"playback_rate": 0.1},
        {"playback_rate": 4.1},
        {"source_crop": {"x": 0.9, "y": 0, "width": 0.2, "height": 1}},
    ],
)
def test_source_controls_reject_out_of_bounds_agent_schema_values(lane, controls):
    from pydantic import ValidationError

    from app.agents._schemas.media_overlay import MediaOverlay
    from app.agents._schemas.visual_block import MediaBlock

    payload = {
        "id": "source-controls",
        "src_gcs_path": "users/test/media.mp4",
        "start_s": 0,
        "end_s": 2,
        **controls,
    }
    if lane == "overlay":
        payload["kind"] = "video"
        model = MediaOverlay
    else:
        payload.update(
            kind="media",
            asset_id="asset-1",
            media_kind="video",
            source_duration_s=8,
            trim_start_s=0,
            trim_end_s=2,
        )
        model = MediaBlock
    with pytest.raises(ValidationError):
        model.model_validate(payload)
