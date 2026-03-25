"""Tests for pipeline/text_overlay.py — PNG generation for editorial text overlays."""

import os

import pytest

from app.pipeline.text_overlay import (
    MAX_OVERLAY_TEXT_LEN,
    generate_text_overlay_png,
)


def _make_overlay(
    text: str = "Test overlay",
    start_s: float = 0.5,
    end_s: float = 2.5,
    position: str = "center",
    effect: str = "none",
) -> dict:
    return {
        "text": text,
        "start_s": start_s,
        "end_s": end_s,
        "position": position,
        "effect": effect,
    }


class TestGenerateTextOverlayPng:
    def test_single_center_overlay(self, tmp_path):
        result = generate_text_overlay_png(
            [_make_overlay()], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        assert len(result) == 1
        assert os.path.exists(result[0]["png_path"])
        assert result[0]["start_s"] == 0.5
        assert result[0]["end_s"] == 2.5

    def test_multiple_positions(self, tmp_path):
        overlays = [
            _make_overlay(text="Top text", position="top"),
            _make_overlay(text="Center text", position="center", start_s=1.0, end_s=3.0),
            _make_overlay(text="Bottom text", position="bottom", start_s=2.0, end_s=4.0),
        ]
        result = generate_text_overlay_png(
            overlays, slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        assert len(result) == 3
        # Each should produce a separate PNG
        paths = {r["png_path"] for r in result}
        assert len(paths) == 3

    def test_png_is_valid_image(self, tmp_path):
        from PIL import Image
        result = generate_text_overlay_png(
            [_make_overlay()], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        img = Image.open(result[0]["png_path"])
        assert img.mode == "RGBA"
        assert img.size == (1080, 1920)

    def test_empty_overlay_list(self, tmp_path):
        result = generate_text_overlay_png(
            [], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is None

    def test_invalid_timing_skipped(self, tmp_path):
        result = generate_text_overlay_png(
            [_make_overlay(start_s=3.0, end_s=2.0)],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is None

    def test_end_clamped_to_slot_duration(self, tmp_path):
        result = generate_text_overlay_png(
            [_make_overlay(start_s=0.0, end_s=10.0)],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        assert result[0]["end_s"] == 5.0

    def test_long_text_truncated(self, tmp_path):
        """60-char text should be truncated — PNG still generated."""
        long_text = "A" * 60
        result = generate_text_overlay_png(
            [_make_overlay(text=long_text)],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        assert os.path.exists(result[0]["png_path"])

    def test_special_chars_sanitized(self, tmp_path):
        result = generate_text_overlay_png(
            [_make_overlay(text=r"{\b1}injected text{\i1}")],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        # Should still render (sanitized to "injected text")
        assert result is not None

    def test_empty_text_skipped(self, tmp_path):
        result = generate_text_overlay_png(
            [_make_overlay(text="   ")],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is None

    def test_png_has_text_pixels(self, tmp_path):
        """The PNG should have non-transparent pixels (the text)."""
        from PIL import Image
        result = generate_text_overlay_png(
            [_make_overlay(text="HELLO")], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        img = Image.open(result[0]["png_path"])
        # Check that some pixels are non-transparent
        alpha = img.split()[3]  # alpha channel
        assert alpha.getextrema()[1] > 0  # max alpha > 0

    def test_different_slot_indices_unique_filenames(self, tmp_path):
        r1 = generate_text_overlay_png(
            [_make_overlay()], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        r2 = generate_text_overlay_png(
            [_make_overlay()], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=1,
        )
        assert r1[0]["png_path"] != r2[0]["png_path"]


class TestFontCycleEffect:
    """Tests for the font-cycle text effect — rapid font switching."""

    def test_font_cycle_produces_multiple_pngs(self, tmp_path):
        """font-cycle effect generates multiple PNGs (one per font frame)."""
        result = generate_text_overlay_png(
            [_make_overlay(text="PERU", effect="font-cycle", start_s=0.0, end_s=2.0)],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        # Should have multiple frames (cycling) + 1 settle frame
        assert len(result) > 3
        # All PNGs exist
        for r in result:
            assert os.path.exists(r["png_path"])

    def test_font_cycle_timing_covers_full_duration(self, tmp_path):
        """The font-cycle frames should cover the full overlay duration."""
        result = generate_text_overlay_png(
            [_make_overlay(text="TOKYO", effect="font-cycle", start_s=0.5, end_s=2.5)],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        # First frame starts at overlay start
        assert result[0]["start_s"] == 0.5
        # Last frame ends at overlay end
        assert result[-1]["end_s"] == 2.5

    def test_font_cycle_no_timing_gaps(self, tmp_path):
        """Each frame's end_s should equal the next frame's start_s (no gaps)."""
        result = generate_text_overlay_png(
            [_make_overlay(text="PARIS", effect="font-cycle", start_s=0.0, end_s=3.0)],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        for i in range(len(result) - 1):
            assert abs(result[i]["end_s"] - result[i + 1]["start_s"]) < 0.001

    def test_font_cycle_pngs_are_valid_images(self, tmp_path):
        """Each font-cycle PNG should be a valid RGBA 1080x1920 image with text."""
        from PIL import Image
        result = generate_text_overlay_png(
            [_make_overlay(text="HELLO", effect="font-cycle", start_s=0.0, end_s=1.5)],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        for r in result:
            img = Image.open(r["png_path"])
            assert img.mode == "RGBA"
            assert img.size == (1080, 1920)
            alpha = img.split()[3]
            assert alpha.getextrema()[1] > 0  # has visible text

    def test_font_cycle_settle_phase(self, tmp_path):
        """The last frame should be the 'settle' frame covering ~30% of duration."""
        result = generate_text_overlay_png(
            [_make_overlay(text="ROME", effect="font-cycle", start_s=0.0, end_s=2.0)],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        last = result[-1]
        # Settle frame should end at 2.0 and cover roughly 30% of duration
        assert last["end_s"] == 2.0
        settle_duration = last["end_s"] - last["start_s"]
        assert settle_duration > 0.4  # at least 0.4s of settle for 2s overlay

    def test_font_cycle_unique_filenames(self, tmp_path):
        """All font-cycle PNGs have unique paths."""
        result = generate_text_overlay_png(
            [_make_overlay(text="NYC", effect="font-cycle", start_s=0.0, end_s=2.0)],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        paths = [r["png_path"] for r in result]
        assert len(paths) == len(set(paths))

    def test_font_cycle_mixed_with_static(self, tmp_path):
        """A font-cycle overlay and a static overlay in the same slot both render."""
        overlays = [
            _make_overlay(text="Welcome to", effect="pop-in", start_s=0.0, end_s=2.0),
            _make_overlay(text="ISTANBUL", effect="font-cycle", start_s=0.0, end_s=2.0),
        ]
        result = generate_text_overlay_png(
            overlays, slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        # Should have 1 static + multiple font-cycle frames
        assert len(result) > 3

    def test_font_cycle_short_duration_still_works(self, tmp_path):
        """Very short font-cycle (0.3s) should still produce at least 2 frames."""
        result = generate_text_overlay_png(
            [_make_overlay(text="HI", effect="font-cycle", start_s=0.0, end_s=0.3)],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        assert len(result) >= 2  # at least 1 cycle + 1 settle
