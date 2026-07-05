"""Tests for the media-overlay schema (slice 1).

Guards:
- Bound clamping (x_frac / y_frac / scale / timing).
- end_s > start_s invariant.
- coerce_media_overlays returns None on empty/garbage (byte-identity contract).
- Position preset resolution.
- GCS path prefix validation.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.media_overlay import (
    MediaOverlay,
    coerce_media_overlays,
    validate_overlay_gcs_path,
)


def _card(**kw) -> dict:
    base = {
        "id": "abc123",
        "kind": "image",
        "src_gcs_path": "users/u1/plan/p1/overlays/card.png",
        "position": "center",
        "scale": 0.35,
        "start_s": 0.0,
        "end_s": 3.0,
        "z": 0,
    }
    base.update(kw)
    return base


class TestMediaOverlayValidation:
    def test_valid_card_parses(self):
        card = MediaOverlay.model_validate(_card())
        assert card.scale == pytest.approx(0.35)
        assert card.end_s == pytest.approx(3.0)

    def test_blank_preview_path_is_absent(self):
        card = MediaOverlay.model_validate(_card(preview_gcs_path="   "))
        assert card.preview_gcs_path is None

    def test_x_frac_clamped_above_1(self):
        card = MediaOverlay.model_validate(_card(x_frac=1.5))
        assert card.x_frac == pytest.approx(1.0)

    def test_y_frac_clamped_below_0(self):
        card = MediaOverlay.model_validate(_card(y_frac=-0.1))
        assert card.y_frac == pytest.approx(0.0)

    def test_scale_clamped_below_minimum(self):
        card = MediaOverlay.model_validate(_card(scale=0.001))
        assert card.scale == pytest.approx(0.05)

    def test_scale_clamped_above_1(self):
        card = MediaOverlay.model_validate(_card(scale=2.0))
        assert card.scale == pytest.approx(1.0)

    def test_start_s_clamped_negative(self):
        card = MediaOverlay.model_validate(_card(start_s=-5.0))
        assert card.start_s == pytest.approx(0.0)

    def test_bad_frac_value_falls_back_to_default(self):
        card = MediaOverlay.model_validate(_card(x_frac="banana"))
        assert 0.0 <= card.x_frac <= 1.0  # clamped / defaulted

    def test_kind_video_accepted(self):
        card = MediaOverlay.model_validate(_card(kind="video"))
        assert card.kind == "video"

    def test_invalid_kind_raises(self):
        with pytest.raises(Exception):
            MediaOverlay.model_validate(_card(kind="sticker"))


class TestPositionPresets:
    @pytest.mark.parametrize("pos,expected_y", [("top", 0.18), ("center", 0.50), ("bottom", 0.82)])
    def test_preset_resolved_y(self, pos, expected_y):
        card = MediaOverlay.model_validate(_card(position=pos))
        _, y = card.resolved_xy_frac()
        assert y == pytest.approx(expected_y)

    @pytest.mark.parametrize("pos", ["top", "center", "bottom"])
    def test_preset_x_is_centered(self, pos):
        card = MediaOverlay.model_validate(_card(position=pos))
        x, _ = card.resolved_xy_frac()
        assert x == pytest.approx(0.5)

    def test_custom_uses_literal_fracs(self):
        card = MediaOverlay.model_validate(_card(position="custom", x_frac=0.3, y_frac=0.7))
        x, y = card.resolved_xy_frac()
        assert x == pytest.approx(0.3)
        assert y == pytest.approx(0.7)

    def test_canvas_center_px_top(self):
        card = MediaOverlay.model_validate(_card(position="top"))
        cx, cy = card.canvas_center_px()
        assert cx == round(0.5 * 1080)
        assert cy == round(0.18 * 1920)

    def test_card_width_px(self):
        card = MediaOverlay.model_validate(_card(scale=0.5))
        assert card.card_width_px() == round(0.5 * 1080)


class TestCoerceMediaOverlays:
    def test_none_input_returns_none(self):
        assert coerce_media_overlays(None) is None

    def test_empty_list_returns_none(self):
        assert coerce_media_overlays([]) is None

    def test_garbage_list_returns_none(self):
        assert coerce_media_overlays(["not-a-dict", 42, None]) is None  # type: ignore[arg-type]

    def test_valid_list_returned(self):
        result = coerce_media_overlays([_card()])
        assert result is not None
        assert len(result) == 1

    def test_mixed_valid_invalid_keeps_valid(self):
        result = coerce_media_overlays([_card(), "bad-entry"])  # type: ignore[list-item]
        assert result is not None
        assert len(result) == 1

    def test_all_bad_returns_none(self):
        result = coerce_media_overlays([{"kind": "invalid_kind", "id": "x"}])
        assert result is None


class TestGcsPathValidation:
    def test_valid_prefix_accepted(self):
        # Should not raise
        validate_overlay_gcs_path("users/u1/plan/p1/overlays/img.png")

    def test_dev_user_prefix_rejected(self):
        with pytest.raises(ValueError, match="users/"):
            validate_overlay_gcs_path("dev-user/abc/raw.mp4")

    def test_generative_jobs_prefix_rejected(self):
        with pytest.raises(ValueError):
            validate_overlay_gcs_path("generative-jobs/abc/out.mp4")

    def test_music_prefix_rejected(self):
        with pytest.raises(ValueError):
            validate_overlay_gcs_path("music/track.mp4")
