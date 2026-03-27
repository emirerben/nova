"""Tests for text_overlay.py -- PNG rendering, font-cycle, and ASS animated effects."""

import os
import tempfile

from app.pipeline.text_overlay import (
    _ASS_OVERLAY_HEADER,
    MAX_OVERLAY_TEXT_LEN,
    OVERLAY_FONT_PATH,
    OVERLAY_FONT_PATH_REGULAR,
    _reset_cycle_cache,
    _resolve_cycle_fonts,
    _validate_ass_file,
    _validate_overlay,
    generate_animated_overlay_ass,
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


# -- _validate_overlay --------------------------------------------------------


class TestValidateOverlay:
    def test_valid_overlay(self):
        text, start, end, pos = _validate_overlay(
            {"text": "Hello", "start_s": 0.0, "end_s": 3.0, "position": "center"},
            5.0,
        )
        assert text == "Hello"
        assert start == 0.0
        assert end == 3.0

    def test_clamps_end_to_slot_duration(self):
        _, _, end, _ = _validate_overlay(
            {"text": "Test", "start_s": 0.0, "end_s": 10.0}, 5.0,
        )
        assert end == 5.0

    def test_skips_when_start_ge_end(self):
        text, _, _, _ = _validate_overlay(
            {"text": "X", "start_s": 5.0, "end_s": 3.0}, 10.0,
        )
        assert text is None

    def test_truncates_long_text(self):
        long_text = "A" * 50
        text, _, _, _ = _validate_overlay(
            {"text": long_text, "start_s": 0.0, "end_s": 3.0}, 5.0,
        )
        assert len(text) == MAX_OVERLAY_TEXT_LEN
        assert text.endswith("\u2026")


# -- ASS animated overlay generation ------------------------------------------


class TestAnimatedOverlayASS:
    def test_fade_in_contains_fad_tag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_animated_overlay_ass(
                [{"text": "Hello", "start_s": 0.0, "end_s": 3.0,
                  "position": "center", "effect": "fade-in"}],
                5.0, tmpdir, 0,
            )
            assert result is not None
            assert len(result) == 1
            with open(result[0]) as f:
                content = f.read()
            assert "\\fad(500,0)" in content

    def test_typewriter_contains_k_tags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_animated_overlay_ass(
                [{"text": "Hi!", "start_s": 0.0, "end_s": 3.0,
                  "position": "center", "effect": "typewriter"}],
                5.0, tmpdir, 0,
            )
            assert result is not None
            with open(result[0]) as f:
                content = f.read()
            assert "\\k" in content
            # Should have one \k tag per character
            assert content.count("\\k") >= 3  # "H", "i", "!"

    def test_slide_up_contains_move_tag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_animated_overlay_ass(
                [{"text": "Slide", "start_s": 0.0, "end_s": 3.0,
                  "position": "center", "effect": "slide-up"}],
                5.0, tmpdir, 0,
            )
            assert result is not None
            with open(result[0]) as f:
                content = f.read()
            assert "\\move(" in content

    def test_ass_header_validation(self):
        """Generated ASS file has all required sections."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_animated_overlay_ass(
                [{"text": "Test", "start_s": 0.0, "end_s": 2.0,
                  "position": "center", "effect": "fade-in"}],
                5.0, tmpdir, 0,
            )
            assert _validate_ass_file(result[0])

    def test_position_mapping_top(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_animated_overlay_ass(
                [{"text": "Top", "start_s": 0.0, "end_s": 2.0,
                  "position": "top", "effect": "fade-in"}],
                5.0, tmpdir, 0,
            )
            with open(result[0]) as f:
                content = f.read()
            assert "\\an8" in content  # top-center alignment

    def test_position_mapping_bottom(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_animated_overlay_ass(
                [{"text": "Bottom", "start_s": 0.0, "end_s": 2.0,
                  "position": "bottom", "effect": "fade-in"}],
                5.0, tmpdir, 0,
            )
            with open(result[0]) as f:
                content = f.read()
            assert "\\an2" in content  # bottom-center alignment

    def test_skips_non_animated_effects(self):
        """generate_animated_overlay_ass ignores static/none effects."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_animated_overlay_ass(
                [{"text": "Static", "start_s": 0.0, "end_s": 3.0,
                  "position": "center", "effect": "static"}],
                5.0, tmpdir, 0,
            )
            assert result is None

    def test_text_truncation_in_ass(self):
        """Long text is truncated in ASS content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_animated_overlay_ass(
                [{"text": "A" * 50, "start_s": 0.0, "end_s": 3.0,
                  "position": "center", "effect": "fade-in"}],
                5.0, tmpdir, 0,
            )
            with open(result[0]) as f:
                content = f.read()
            # Text should be truncated to MAX_OVERLAY_TEXT_LEN
            assert "\u2026" in content


# -- PNG overlay generation ---------------------------------------------------


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
        """60-char text should be truncated -- PNG still generated."""
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

    def test_renders_animated_effects_as_png_fallback(self):
        """generate_text_overlay_png renders ALL overlays including animated (as fallback)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_text_overlay_png(
                [{"text": "Fade", "start_s": 0.0, "end_s": 3.0,
                  "position": "center", "effect": "fade-in"}],
                5.0, tmpdir, 0,
            )
            assert result is not None
            assert len(result) == 1

    def test_renders_static_overlay(self):
        """Static effect produces a PNG file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_text_overlay_png(
                [{"text": "Hello", "start_s": 0.0, "end_s": 3.0,
                  "position": "center", "effect": "static"}],
                5.0, tmpdir, 0,
            )
            assert result is not None
            assert len(result) == 1
            assert result[0]["png_path"].endswith(".png")
            assert os.path.exists(result[0]["png_path"])


class TestFontCycleEffect:
    """Tests for the font-cycle text effect -- rapid font switching."""

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


# -- Playfair Display font tests ----------------------------------------------


class TestPlayfairDisplayFonts:
    """Tests for the Playfair Display font bundle and configuration."""

    def test_playfair_bold_loads(self):
        """Primary font (Playfair Display Bold) is bundled and loadable."""
        from PIL import ImageFont

        assert os.path.exists(OVERLAY_FONT_PATH), f"Missing: {OVERLAY_FONT_PATH}"
        font = ImageFont.truetype(OVERLAY_FONT_PATH, 90)
        family, style = font.getname()
        assert family == "Playfair Display"
        assert style == "Bold"

    def test_playfair_regular_loads(self):
        """Serif font (Playfair Display Regular) is bundled and loadable."""
        from PIL import ImageFont

        assert os.path.exists(OVERLAY_FONT_PATH_REGULAR), f"Missing: {OVERLAY_FONT_PATH_REGULAR}"
        font = ImageFont.truetype(OVERLAY_FONT_PATH_REGULAR, 72)
        family, style = font.getname()
        assert family == "Playfair Display"
        assert style == "Regular"

    def test_display_style_renders(self, tmp_path):
        """The 'display' font_style (Playfair Bold) renders a valid PNG."""
        from PIL import Image

        result = generate_text_overlay_png(
            [{"text": "PORTUGAL", "start_s": 0.0, "end_s": 3.0,
              "position": "center", "effect": "none", "font_style": "display",
              "text_size": "large", "text_color": "#FFFFFF"}],
            5.0, str(tmp_path), 0,
        )
        assert result is not None
        img = Image.open(result[0]["png_path"])
        assert img.mode == "RGBA"
        assert img.size == (1080, 1920)
        alpha = img.split()[3]
        assert alpha.getextrema()[1] > 0  # has visible text

    def test_serif_style_renders(self, tmp_path):
        """The 'serif' font_style (Playfair Regular) renders a valid PNG."""
        result = generate_text_overlay_png(
            [{"text": "Welcome to", "start_s": 0.0, "end_s": 3.0,
              "position": "top", "effect": "none", "font_style": "serif",
              "text_size": "medium", "text_color": "#FFFFFF"}],
            5.0, str(tmp_path), 0,
        )
        assert result is not None
        assert os.path.exists(result[0]["png_path"])

    def test_ass_header_uses_playfair(self):
        """ASS overlay header should reference Playfair Display, not Montserrat."""
        assert "Playfair Display" in _ASS_OVERLAY_HEADER
        assert "Montserrat" not in _ASS_OVERLAY_HEADER

    def test_cycle_cache_reset(self):
        """Font-cycle cache can be reset and rebuilt."""
        _reset_cycle_cache()
        fonts = _resolve_cycle_fonts()
        assert len(fonts) >= 1  # at least Playfair Bold is bundled
        _reset_cycle_cache()  # clean up
