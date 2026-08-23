from __future__ import annotations

import pytest

from app.agents._schemas.visual_block import (
    MediaBlock,
    MediaTransform,
    MontageBlock,
    SyncAnchor,
    retime_montage,
    validate_visual_block_text_links,
    validate_visual_blocks,
)


def _media(*, media_kind: str = "image", start: float = 0.0, end: float = 1.0, **extra) -> dict:
    payload = {
        "version": 1,
        "id": f"media-{media_kind}-{start}",
        "kind": "media",
        "asset_id": f"asset-{media_kind}",
        "src_gcs_path": f"users/u/media.{'jpg' if media_kind == 'image' else 'mp4'}",
        "media_kind": media_kind,
        "start_s": start,
        "end_s": end,
        "timing_mode": "manual",
        "origin": "user",
        "audio_policy": {"base": "continue", "sfx": "continue"},
    }
    if media_kind == "video":
        payload["source_duration_s"] = 8.0
    payload.update(extra)
    return payload


def _shot(index: int, *, offset: float, duration: float) -> dict:
    return {
        "id": f"shot-{index}",
        "asset_id": f"asset-{index}",
        "src_gcs_path": f"users/u/plan/p/pool/{index}.jpg",
        "kind": "image",
        "start_offset_s": offset,
        "duration_s": duration,
        "crop": {"x_frac": 0.5, "y_frac": 0.5, "scale": 1.0},
        "motion": "zoom_in",
    }


def _montage(*, start: float = 0.0) -> dict:
    return {
        "version": 1,
        "id": "montage-1",
        "kind": "montage",
        "start_s": start,
        "end_s": start + 3.0,
        "timing_mode": "manual",
        "origin": "user",
        "transition_in": "cut",
        "transition_out": "fade",
        "audio_policy": {"base": "continue", "sfx": "mute"},
        "shots": [
            _shot(1, offset=0.0, duration=1.0),
            _shot(2, offset=1.0, duration=1.0),
            _shot(3, offset=2.0, duration=1.0),
        ],
    }


def test_strict_validation_accepts_contiguous_montage() -> None:
    result = validate_visual_blocks([_montage()], duration_s=8.0)
    assert result[0]["kind"] == "montage"
    assert result[0]["audio_policy"]["sfx"] == "mute"


def test_strict_validation_rejects_overlap() -> None:
    second = _montage(start=2.5)
    second["id"] = "montage-2"
    with pytest.raises(ValueError, match="overlap"):
        validate_visual_blocks([_montage(), second], duration_s=10.0)


def test_media_transform_defaults_and_bounds() -> None:
    transform = MediaTransform()
    assert transform.fit_mode == "contain"
    assert transform.focal_x == pytest.approx(0.5)
    assert transform.focal_y == pytest.approx(0.5)
    assert transform.zoom == pytest.approx(1.0)
    MediaTransform(focal_x=0.0, focal_y=1.0, zoom=4.0)
    with pytest.raises(ValueError):
        MediaTransform(zoom=4.01)


def test_media_blocks_may_overlap_while_structured_overlap_is_rejected() -> None:
    result = validate_visual_blocks(
        [_media(start=0.0, end=2.0), _media(start=1.0, end=3.0, id="media-2")],
        duration_s=4.0,
    )
    assert len(result) == 2
    second = _montage(start=1.0)
    second["id"] = "montage-2"
    with pytest.raises(ValueError, match="structured visual blocks may not overlap"):
        validate_visual_blocks([_montage(), second], duration_s=8.0)


def test_media_window_has_point_one_second_minimum() -> None:
    with pytest.raises(ValueError, match="at least 0.1s"):
        MediaBlock.model_validate(_media(start=1.0, end=1.09))


def test_video_trim_end_clamps_to_source_and_selected_window_is_checked() -> None:
    block = MediaBlock.model_validate(
        _media(
            media_kind="video",
            start=0.0,
            end=1.0,
            source_duration_s=2.0,
            trim_start_s=0.0,
            trim_end_s=10.0,
        )
    )
    assert block.trim_end_s == pytest.approx(2.0)
    with pytest.raises(ValueError, match="exceeds the selected source footage"):
        MediaBlock.model_validate(
            _media(
                media_kind="video",
                start=0.0,
                end=2.0,
                source_duration_s=3.0,
                trim_start_s=1.0,
                trim_end_s=2.0,
            )
        )


def test_text_card_requires_linked_text_inside_window() -> None:
    card = {
        "version": 1,
        "id": "card-1",
        "kind": "text_card",
        "start_s": 4.0,
        "end_s": 6.0,
        "timing_mode": "manual",
        "origin": "user",
        "transition_in": "cut",
        "transition_out": "cut",
        "audio_policy": {"base": "continue", "sfx": "continue"},
        "background": {"type": "solid", "color": "#112233"},
    }
    blocks = validate_visual_blocks([card], duration_s=8.0)
    with pytest.raises(ValueError, match="require"):
        validate_visual_block_text_links(blocks, [])
    validate_visual_block_text_links(
        blocks,
        [
            {
                "id": "text-1",
                "visual_block_id": "card-1",
                "start_s": 4.0,
                "end_s": 6.0,
            }
        ],
    )


def test_retime_persists_concrete_offsets() -> None:
    block = MontageBlock.model_validate(_montage())
    retimed = retime_montage(block.model_copy(update={"end_s": 4.5}))
    assert retimed.timing_mode == "auto"
    assert [shot.start_offset_s for shot in retimed.shots] == [0.0, 1.5, 3.0]
    assert sum(shot.duration_s for shot in retimed.shots) == pytest.approx(4.5)


def test_retime_prefers_sentence_and_beat_boundaries_near_even_pacing() -> None:
    block = MontageBlock.model_validate(_montage())
    retimed = retime_montage(
        block,
        anchors=[
            SyncAnchor(type="sentence", time_s=0.85, label="Sentence end"),
            SyncAnchor(type="beat", time_s=2.1),
        ],
    )
    assert [shot.start_offset_s for shot in retimed.shots] == [0.0, 0.85, 2.1]
    assert retimed.shots[1].sync_anchor.type == "sentence"
    assert retimed.shots[2].sync_anchor.type == "beat"
