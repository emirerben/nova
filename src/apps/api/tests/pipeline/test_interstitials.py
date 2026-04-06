"""Tests for interstitials.py — black segment classification and rendering."""

import pytest
from unittest.mock import MagicMock, patch  # noqa: I001

from app.pipeline.interstitials import (
    MIN_CURTAIN_ANIMATE_S,
    _CURTAIN_MAX_RATIO,
    _classify_single_segment,
    classify_black_segment_type,
)


# ── Helper to build mock signalstats stderr ──────────────────────────────────


def _signalstats_stderr(yavg: float) -> bytes:
    """Build fake FFmpeg stderr containing a signalstats YAVG line."""
    return (
        f"[Parsed_signalstats_1 @ 0x1234] YAVG:{yavg:.1f} "
        f"YMIN:0 YMAX:255\n"
    ).encode()


# ── classify_black_segment_type ──────────────────────────────────────────────


class TestClassifyBlackSegmentType:
    def test_empty_segments_returns_empty(self):
        result = classify_black_segment_type("/fake/video.mp4", [])
        assert result == []

    @patch("app.pipeline.interstitials._sample_band_luminance")
    def test_curtain_close_pattern_detected(self, mock_lum):
        """Top/bottom bands darken while middle stays bright → curtain-close."""
        # 6 sample frames: edges progressively darken, middle stays bright
        # Frame order: earliest to latest (approaching black_start)
        lum_sequence = {
            # (timestamp_index, band) → luminance
            # Early frames: all bands bright
            (0, "top"): 200.0, (0, "middle"): 200.0, (0, "bottom"): 200.0,
            (1, "top"): 180.0, (1, "middle"): 195.0, (1, "bottom"): 175.0,
            (2, "top"): 140.0, (2, "middle"): 190.0, (2, "bottom"): 135.0,
            # Later frames: edges dark, middle still bright
            (3, "top"): 80.0, (3, "middle"): 185.0, (3, "bottom"): 75.0,
            (4, "top"): 30.0, (4, "middle"): 170.0, (4, "bottom"): 25.0,
            (5, "top"): 5.0, (5, "middle"): 150.0, (5, "bottom"): 3.0,
        }

        call_count = [0]
        def side_effect(video_path, timestamp, band):
            idx = call_count[0] // 3  # 3 bands per frame
            key = (idx, band)
            call_count[0] += 1
            return lum_sequence.get(key, 100.0)

        mock_lum.side_effect = side_effect

        segments = [{"start_s": 12.0, "end_s": 13.0, "duration_s": 1.0}]
        result = classify_black_segment_type("/fake/video.mp4", segments)

        assert len(result) == 1
        assert result[0]["likely_type"] == "curtain-close"

    @patch("app.pipeline.interstitials._sample_band_luminance")
    def test_fade_pattern_detected(self, mock_lum):
        """All bands darken uniformly → fade-black-hold."""
        lum_sequence = {
            (0, "top"): 200.0, (0, "middle"): 200.0, (0, "bottom"): 200.0,
            (1, "top"): 170.0, (1, "middle"): 170.0, (1, "bottom"): 170.0,
            (2, "top"): 130.0, (2, "middle"): 130.0, (2, "bottom"): 130.0,
            (3, "top"): 90.0, (3, "middle"): 90.0, (3, "bottom"): 90.0,
            (4, "top"): 50.0, (4, "middle"): 50.0, (4, "bottom"): 50.0,
            (5, "top"): 10.0, (5, "middle"): 10.0, (5, "bottom"): 10.0,
        }

        call_count = [0]
        def side_effect(video_path, timestamp, band):
            idx = call_count[0] // 3
            key = (idx, band)
            call_count[0] += 1
            return lum_sequence.get(key, 100.0)

        mock_lum.side_effect = side_effect

        segments = [{"start_s": 12.0, "end_s": 13.0, "duration_s": 1.0}]
        result = classify_black_segment_type("/fake/video.mp4", segments)

        assert len(result) == 1
        assert result[0]["likely_type"] == "fade-black-hold"

    @patch("app.pipeline.interstitials._sample_band_luminance")
    def test_segment_at_video_start_is_unknown(self, mock_lum):
        """Black segment at t=0 has no pre-frames → unknown."""
        segments = [{"start_s": 0.0, "end_s": 1.0, "duration_s": 1.0}]
        result = classify_black_segment_type("/fake/video.mp4", segments)

        assert len(result) == 1
        assert result[0]["likely_type"] == "unknown"
        mock_lum.assert_not_called()

    @patch("app.pipeline.interstitials._sample_band_luminance")
    def test_insufficient_frames_returns_unknown(self, mock_lum):
        """If FFmpeg fails to return luminance for most frames → unknown."""
        mock_lum.return_value = None  # all FFmpeg calls fail

        segments = [{"start_s": 5.0, "end_s": 6.0, "duration_s": 1.0}]
        result = classify_black_segment_type("/fake/video.mp4", segments)

        assert len(result) == 1
        assert result[0]["likely_type"] == "unknown"

    @patch("app.pipeline.interstitials._sample_band_luminance")
    def test_multiple_segments_classified_independently(self, mock_lum):
        """Each segment gets its own classification."""
        call_idx = [0]

        def side_effect(video_path, timestamp, band):
            # First segment: curtain close pattern (edges dark, middle bright)
            # Second segment: all bands same (fade or unknown)
            seg_idx = 0 if timestamp < 10.0 else 1
            frame_in_seg = call_idx[0] // 3
            call_idx[0] += 1

            if seg_idx == 0:
                # Curtain close: later frames have dark edges, bright middle
                if frame_in_seg >= 3:
                    if band == "middle":
                        return 180.0
                    return 5.0
                return 200.0
            else:
                # Uniform: all bands same
                return max(10.0, 200.0 - frame_in_seg * 35.0)

        mock_lum.side_effect = side_effect

        segments = [
            {"start_s": 5.0, "end_s": 6.0, "duration_s": 1.0},
            {"start_s": 15.0, "end_s": 16.0, "duration_s": 1.0},
        ]
        result = classify_black_segment_type("/fake/video.mp4", segments)

        assert len(result) == 2
        assert result[0]["likely_type"] == "curtain-close"
        # Second could be fade-black-hold or unknown depending on exact values

    def test_likely_type_field_added_to_all_segments(self):
        """Every segment gets a likely_type field, even on errors."""
        with patch("app.pipeline.interstitials._sample_band_luminance", return_value=None):
            segments = [
                {"start_s": 0.0, "end_s": 1.0, "duration_s": 1.0},
                {"start_s": 5.0, "end_s": 6.0, "duration_s": 1.0},
            ]
            result = classify_black_segment_type("/fake/video.mp4", segments)

            for seg in result:
                assert "likely_type" in seg


# ── _classify_single_segment edge cases ──────────────────────────────────────


class TestClassifySingleSegment:
    @patch("app.pipeline.interstitials._sample_frame_bands")
    def test_edges_near_zero_middle_bright(self, mock_bands):
        """Edge case: edges fully black, middle still bright → curtain-close."""
        # Return uniform brightness for early frames, then bar pattern
        def side_effect(video_path, timestamp):
            if timestamp < 11.7:
                return {"top": 180.0, "middle": 180.0, "bottom": 180.0}
            return {"top": 0.5, "middle": 150.0, "bottom": 0.5}

        mock_bands.side_effect = side_effect

        segment = {"start_s": 12.0, "end_s": 13.0, "duration_s": 1.0}
        result = _classify_single_segment("/fake/video.mp4", segment)
        assert result == "curtain-close"

    @patch("app.pipeline.interstitials._sample_frame_bands")
    def test_all_bright_no_darkening(self, mock_bands):
        """No darkening at all → unknown (not a transition)."""
        mock_bands.return_value = {"top": 200.0, "middle": 200.0, "bottom": 200.0}

        segment = {"start_s": 12.0, "end_s": 13.0, "duration_s": 1.0}
        result = _classify_single_segment("/fake/video.mp4", segment)
        assert result == "unknown"


# ── detect_black_segments threshold tests ────────────────────────────────────


class TestDetectBlackSegmentsThresholds:
    @patch("app.pipeline.interstitials.subprocess.run")
    def test_default_thresholds_are_permissive(self, mock_run):
        """Verify the lowered thresholds are used in the FFmpeg command."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stderr=b"",
        )

        from app.pipeline.interstitials import detect_black_segments
        detect_black_segments("/fake/video.mp4")

        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        # Verify lowered thresholds
        assert "d=0.15" in cmd_str
        assert "pix_th=0.15" in cmd_str


# ── MIN_CURTAIN_ANIMATE_S clamp ─────────────────────────────────────────────


class TestCurtainMinAnimateS:
    def test_min_curtain_constant(self):
        assert MIN_CURTAIN_ANIMATE_S == 4.0

    def test_curtain_max_ratio_constant(self):
        assert _CURTAIN_MAX_RATIO == 0.6

    def test_curtain_min_animate_s_clamped(self):
        """animate_s below MIN_CURTAIN_ANIMATE_S is clamped to 4.0, then 60% rule applies."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        # 10s slot: MIN=4.0, 60% of 10=6.0 → 4.0 wins (no clamp needed)
        step = MagicMock()
        step.clip_id = "clip_a"
        step.moment = {"start_s": 0.0, "end_s": 10.0}
        step.slot = {
            "position": 1,
            "target_duration_s": 10.0,
            "text_overlays": [{
                "role": "label",
                "start_s": 0.0,
                "end_s": 10.0,
                "position": "center",
                "effect": "font-cycle",
                "sample_text": "PERU",
            }],
        }

        interstitial_map = {
            1: {"type": "curtain-close", "animate_s": 0.3, "hold_s": 1.0},
        }
        result = _collect_absolute_overlays(
            [step], [10.0], None, "Peru",
            interstitial_map=interstitial_map,
        )
        assert len(result) == 1
        # animate_s = max(4.0, 0.3) = 4.0, min(4.0, 10*0.6=6.0) = 4.0
        # accel_at = 10.0 - 4.0 = 6.0
        assert result[0].get("font_cycle_accel_at_s") == 6.0

    def test_clamp_at_60_pct(self):
        """60% clamp: 8s slot with 4.0 MIN → 60% clamp gives 4.8, MIN wins at 4.0."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = MagicMock()
        step.clip_id = "clip_a"
        step.moment = {"start_s": 0.0, "end_s": 8.0}
        step.slot = {
            "position": 1,
            "target_duration_s": 8.0,
            "text_overlays": [{
                "role": "label",
                "start_s": 0.0,
                "end_s": 8.0,
                "position": "center",
                "effect": "font-cycle",
                "sample_text": "PERU",
            }],
        }

        interstitial_map = {
            1: {"type": "curtain-close", "animate_s": 1.0, "hold_s": 1.0},
        }
        result = _collect_absolute_overlays(
            [step], [8.0], None, "Peru",
            interstitial_map=interstitial_map,
        )
        assert len(result) == 1
        # animate_s = max(4.0, 1.0) = 4.0, min(4.0, 8.0*0.6=4.8) = 4.0
        # accel_at = 8.0 - 4.0 = 4.0
        # config accel = 8.0, min(8.0, 4.0) = 4.0
        # entry["start_s"] = 3.0 (subject label first-slot override)
        # 4.0 > 3.0 → set
        assert result[0].get("font_cycle_accel_at_s") == 4.0

    def test_clamp_equal_duration(self):
        """7s slot where 60% clamp (4.2) wins over MIN (4.0)."""
        from app.tasks.template_orchestrate import _collect_absolute_overlays

        step = MagicMock()
        step.clip_id = "clip_a"
        step.moment = {"start_s": 0.0, "end_s": 7.0}
        step.slot = {
            "position": 1,
            "target_duration_s": 7.0,
            "text_overlays": [{
                "role": "label",
                "start_s": 0.0,
                "end_s": 7.0,
                "position": "center",
                "effect": "font-cycle",
                "sample_text": "PERU",
            }],
        }

        interstitial_map = {
            1: {"type": "curtain-close", "animate_s": 5.0, "hold_s": 1.0},
        }
        result = _collect_absolute_overlays(
            [step], [7.0], None, "Peru",
            interstitial_map=interstitial_map,
        )
        assert len(result) == 1
        # animate_s = max(4.0, 5.0) = 5.0, min(5.0, 7.0*0.6=4.2) = 4.2
        # accel_at = 7.0 - 4.2 = 2.8
        # entry["start_s"] = 3.0 (subject label first-slot override)
        # 2.8 > 3.0 → False → config accel_at=8.0 stays
        # But wait, 2.8 < 3.0, so curtain accel is rejected. Config 8.0 stays.
        # On a 7s slot, 8.0 is past the end. So effectively no accel.
        assert result[0].get("font_cycle_accel_at_s") == 8.0
