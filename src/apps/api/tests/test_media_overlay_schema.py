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


class TestDisplayMode:
    """Plan 009 T1: display_mode field + coercing validator (version-skew safe)."""

    def test_default_is_pip(self):
        card = MediaOverlay.model_validate(_card())
        assert card.display_mode == "pip"

    def test_explicit_fullscreen_parses(self):
        card = MediaOverlay.model_validate(_card(display_mode="fullscreen"))
        assert card.display_mode == "fullscreen"

    def test_unknown_value_coerces_to_pip_never_drops(self):
        # A card from a NEWER client with an unknown mode must not be dropped.
        card = MediaOverlay.model_validate(_card(display_mode="cinema"))
        assert card.display_mode == "pip"

    def test_non_string_coerces_to_pip(self):
        card = MediaOverlay.model_validate(_card(display_mode=7))
        assert card.display_mode == "pip"

    def test_fullscreen_preserves_pip_layout_fields(self):
        # Toggle-back contract: fracs/scale ride along untouched in the dict.
        raw = _card(
            display_mode="fullscreen",
            position="custom",
            x_frac=0.3,
            y_frac=0.7,
            scale=0.42,
        )
        card = MediaOverlay.model_validate(raw)
        dumped = card.model_dump()
        assert dumped["display_mode"] == "fullscreen"
        assert dumped["x_frac"] == pytest.approx(0.3)
        assert dumped["y_frac"] == pytest.approx(0.7)
        assert dumped["scale"] == pytest.approx(0.42)

    def test_round_trip_through_coerce(self):
        cards = coerce_media_overlays([_card(display_mode="fullscreen")])
        assert cards is not None
        assert cards[0].display_mode == "fullscreen"


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


class TestIdDefaultAssignment:
    """Prod 2026-07-12: `id` was required, so a PUT payload without ids
    validated to [] (coerce dropped every card) and the route 200'd having
    cleared the user's cards. `id` is now server-assigned on parse, mirroring
    TextElement.id."""

    def test_missing_id_gets_server_assigned_uuid_hex(self):
        raw = _card()
        del raw["id"]
        card = MediaOverlay.model_validate(raw)
        assert card.id
        assert len(card.id) == 32
        int(card.id, 16)  # valid hex

    def test_provided_id_is_preserved(self):
        card = MediaOverlay.model_validate(_card(id="deadbeef"))
        assert card.id == "deadbeef"

    def test_assigned_ids_are_unique_per_card(self):
        raw1, raw2 = _card(), _card()
        del raw1["id"]
        del raw2["id"]
        c1 = MediaOverlay.model_validate(raw1)
        c2 = MediaOverlay.model_validate(raw2)
        assert c1.id != c2.id

    def test_coerce_keeps_all_cards_without_ids(self):
        # The prod repro: two id-less cards must round-trip, not drop to None.
        raw1, raw2 = _card(), _card()
        del raw1["id"]
        del raw2["id"]
        result = coerce_media_overlays([raw1, raw2])
        assert result is not None
        assert len(result) == 2
        assert all(c.id for c in result)


class TestCoerceDroppedIndices:
    def test_no_drops_leaves_list_empty(self):
        dropped: list[int] = []
        result = coerce_media_overlays([_card()], dropped_indices=dropped)
        assert result is not None
        assert dropped == []

    def test_reports_indices_of_dropped_entries(self):
        dropped: list[int] = []
        result = coerce_media_overlays(
            [_card(), "not-a-dict", _card(kind="sticker"), _card()],  # type: ignore[list-item]
            dropped_indices=dropped,
        )
        assert result is not None
        assert len(result) == 2
        assert dropped == [1, 2]

    def test_all_dropped_returns_none_with_all_indices(self):
        dropped: list[int] = []
        result = coerce_media_overlays(
            [{"kind": "sticker"}, 42],
            dropped_indices=dropped,  # type: ignore[list-item]
        )
        assert result is None
        assert dropped == [0, 1]


class TestUserFacingValidatorRejectsDrops:
    """validate_media_overlays_for_user must 422 (never silent-200) when any
    card in a full-replace payload fails schema validation."""

    @staticmethod
    def _validate(raw: list[dict]) -> list[dict]:
        from app.routes.generative_jobs import validate_media_overlays_for_user

        return validate_media_overlays_for_user(overlays_raw=raw, user_id="u1")

    def test_payload_without_ids_round_trips_all_cards(self):
        raw1, raw2 = _card(), _card(start_s=5.0, end_s=8.0)
        del raw1["id"]
        del raw2["id"]
        validated = self._validate([raw1, raw2])
        assert len(validated) == 2
        assert all(v["id"] for v in validated)

    def test_malformed_card_raises_422_with_count(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            self._validate([_card(), _card(kind="sticker")])
        assert exc_info.value.status_code == 422
        assert "1 of 2" in exc_info.value.detail
        assert "indices: [1]" in exc_info.value.detail

    def test_all_malformed_raises_422_not_empty_list(self):
        # The exact prod repro shape: every card invalid → used to return []
        # (silent full clear). Must now 422.
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            self._validate([{"kind": "sticker"}, {"kind": "hologram"}])
        assert exc_info.value.status_code == 422
        assert "2 of 2" in exc_info.value.detail


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
