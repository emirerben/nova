"""Tests for text_overlay.py -- PNG rendering, font-cycle, and ASS animated effects."""

import os
import tempfile

from app.pipeline.text_overlay import (
    _ASS_OVERLAY_HEADER,
    MAX_OVERLAY_TEXT_LEN,
    OVERLAY_FONT_PATH,
    OVERLAY_FONT_PATH_REGULAR,
    _build_ass_header,
    _reset_cycle_cache,
    _resolve_cycle_fonts,
    _resolve_font_family,
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
        long_text = "A" * (MAX_OVERLAY_TEXT_LEN + 50)
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
                [{"text": "A" * (MAX_OVERLAY_TEXT_LEN + 50), "start_s": 0.0, "end_s": 3.0,
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

    def test_font_cycle_frame_cap_fills_gap(self, tmp_path):
        """When frame cap is hit, gap-fill PNG bridges cycling to settle phase."""
        from app.pipeline.text_overlay import FONT_CYCLE_INTERVAL_S, MAX_FONT_CYCLE_FRAMES
        # Create an overlay long enough that the frame cap is hit during cycling.
        # cycling covers 70% of duration; at 0.15s per frame, cap needs
        # duration * 0.7 / 0.15 > MAX_FONT_CYCLE_FRAMES
        min_duration = (MAX_FONT_CYCLE_FRAMES * FONT_CYCLE_INTERVAL_S) / 0.7 + 1.0
        result = generate_text_overlay_png(
            [_make_overlay(text="LONG", effect="font-cycle", start_s=0.0, end_s=min_duration)],
            slot_duration_s=min_duration + 1.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        # Verify no timing gaps between any consecutive frames
        for i in range(len(result) - 1):
            gap = result[i + 1]["start_s"] - result[i]["end_s"]
            assert abs(gap) < 0.001, f"Gap of {gap:.4f}s between frame {i} and {i+1}"
        # First frame starts at overlay start, last ends at overlay end
        assert result[0]["start_s"] == 0.0
        assert abs(result[-1]["end_s"] - min_duration) < 0.001


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


# -- text_color support -------------------------------------------------------


class TestFontCycleTextColor:
    """Tests for custom text_color in font-cycle and static overlays."""

    def test_font_cycle_respects_text_color(self, tmp_path):
        """Font-cycle PNGs should use the specified text_color (yellow)."""
        from PIL import Image

        overlay = {
            "text": "PERU",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "center",
            "effect": "font-cycle",
            "text_color": "#FFD700",
        }
        result = generate_text_overlay_png(
            [overlay], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        # Check that PNGs contain yellow-ish pixels (R > 200, G > 150)
        img = Image.open(result[0]["png_path"])
        pixels = list(img.getdata())
        visible = [p for p in pixels if p[3] > 100]  # non-transparent
        # At least some visible pixels should have yellow tones
        yellow_pixels = [p for p in visible if p[0] > 200 and p[1] > 150 and p[2] < 100]
        assert len(yellow_pixels) > 0, "No yellow pixels found in font-cycle PNG"

    def test_static_overlay_respects_text_color(self, tmp_path):
        """Static overlays use text_color from the overlay dict."""
        from PIL import Image

        overlay = {
            "text": "Hello",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "center",
            "effect": "none",
            "text_color": "#FF0000",
        }
        result = generate_text_overlay_png(
            [overlay], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        img = Image.open(result[0]["png_path"])
        pixels = list(img.getdata())
        visible = [p for p in pixels if p[3] > 100]
        red_pixels = [p for p in visible if p[0] > 200 and p[1] < 50 and p[2] < 50]
        assert len(red_pixels) > 0, "No red pixels found"

    def test_default_text_color_is_white(self, tmp_path):
        """Without text_color, font-cycle defaults to white."""
        from PIL import Image

        overlay = {
            "text": "TOKYO",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "center",
            "effect": "font-cycle",
        }
        result = generate_text_overlay_png(
            [overlay], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        img = Image.open(result[0]["png_path"])
        pixels = list(img.getdata())
        visible = [p for p in pixels if p[3] > 100]
        # White pixels: R, G, B all high
        white_pixels = [p for p in visible if p[0] > 200 and p[1] > 200 and p[2] > 200]
        assert len(white_pixels) > 0, "No white pixels found (default color)"


# -- font-cycle acceleration --------------------------------------------------


class TestFontCycleAcceleration:
    """Tests for font-cycle speed acceleration (curtain-close sync)."""

    def test_acceleration_produces_more_frames(self, tmp_path):
        """With accel_at_s, the fast phase should produce more frames than normal."""
        normal_dir = tmp_path / "normal"
        accel_dir = tmp_path / "accel"
        os.makedirs(normal_dir, exist_ok=True)
        os.makedirs(accel_dir, exist_ok=True)

        # Normal speed: 3s overlay
        normal = generate_text_overlay_png(
            [{"text": "PERU", "start_s": 0.0, "end_s": 3.0,
              "position": "center", "effect": "font-cycle"}],
            slot_duration_s=5.0,
            output_dir=str(normal_dir), slot_index=0,
        )

        # Accelerated: same 3s overlay, fast after 1.0s
        accel = generate_text_overlay_png(
            [{"text": "PERU", "start_s": 0.0, "end_s": 3.0,
              "position": "center", "effect": "font-cycle",
              "font_cycle_accel_at_s": 1.0}],
            slot_duration_s=5.0,
            output_dir=str(accel_dir), slot_index=0,
        )

        assert normal is not None
        assert accel is not None
        # Accelerated version should have more frames (faster switching = more PNGs)
        assert len(accel) > len(normal)

    def test_acceleration_no_timing_gaps(self, tmp_path):
        """Accelerated font-cycle frames still have no timing gaps."""
        result = generate_text_overlay_png(
            [{"text": "ROME", "start_s": 0.0, "end_s": 3.0,
              "position": "center", "effect": "font-cycle",
              "font_cycle_accel_at_s": 1.5}],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        for i in range(len(result) - 1):
            assert abs(result[i]["end_s"] - result[i + 1]["start_s"]) < 0.001

    def test_acceleration_covers_full_duration(self, tmp_path):
        """Accelerated overlay still covers start to end."""
        result = generate_text_overlay_png(
            [{"text": "PARIS", "start_s": 1.0, "end_s": 4.0,
              "position": "center", "effect": "font-cycle",
              "font_cycle_accel_at_s": 2.5}],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        assert result[0]["start_s"] == 1.0
        assert result[-1]["end_s"] == 4.0

    def test_font_cycle_accel_skips_settle_phase(self, tmp_path):
        """With accel_at_s, no settle PNG — cycling extends to end_s."""
        result = generate_text_overlay_png(
            [{"text": "ROME", "start_s": 0.0, "end_s": 3.0,
              "position": "center", "effect": "font-cycle",
              "font_cycle_accel_at_s": 1.5}],
            slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        # No settle PNG should exist — all PNGs should be cycling frames
        settle_pngs = [r for r in result if "settle" in r["png_path"]]
        assert len(settle_pngs) == 0, "Settle PNG should not be generated when accel is active"
        # Last frame should end at overlay end
        assert result[-1]["end_s"] == 3.0
        # Frames after accel_at should use fast interval (0.07s)
        fast_frames = [r for r in result if r["start_s"] >= 1.5 and "gapfill" not in r["png_path"]]
        if len(fast_frames) >= 2:
            interval = fast_frames[1]["start_s"] - fast_frames[0]["start_s"]
            assert interval < 0.10, f"Expected fast interval (~0.07s), got {interval:.3f}s"


# -- font-cycle text_size support ---------------------------------------------


class TestFontCycleTextSize:
    """Tests for font-cycle respecting the text_size field."""

    def test_large_size_produces_larger_text(self, tmp_path):
        """text_size='large' should render bigger text than 'small'."""
        from PIL import Image

        small_dir = tmp_path / "small"
        large_dir = tmp_path / "large"
        os.makedirs(small_dir, exist_ok=True)
        os.makedirs(large_dir, exist_ok=True)

        small = generate_text_overlay_png(
            [{"text": "HI", "start_s": 0.0, "end_s": 2.0,
              "position": "center", "effect": "font-cycle",
              "text_size": "small"}],
            slot_duration_s=5.0,
            output_dir=str(small_dir), slot_index=0,
        )
        large = generate_text_overlay_png(
            [{"text": "HI", "start_s": 0.0, "end_s": 2.0,
              "position": "center", "effect": "font-cycle",
              "text_size": "large"}],
            slot_duration_s=5.0,
            output_dir=str(large_dir), slot_index=0,
        )

        assert small is not None
        assert large is not None

        # Count non-transparent pixels as a proxy for text size
        small_img = Image.open(small[0]["png_path"])
        large_img = Image.open(large[0]["png_path"])

        small_visible = sum(1 for p in small_img.getdata() if p[3] > 50)
        large_visible = sum(1 for p in large_img.getdata() if p[3] > 50)

        assert large_visible > small_visible, (
            f"Large text ({large_visible}px) should have more visible pixels "
            f"than small ({small_visible}px)"
        )

    def test_resolve_cycle_fonts_caches_by_size(self):
        """_resolve_cycle_fonts returns different font objects for different sizes."""
        _reset_cycle_cache()
        fonts_72 = _resolve_cycle_fonts(72)
        fonts_120 = _resolve_cycle_fonts(120)
        assert len(fonts_72) >= 1
        assert len(fonts_120) >= 1
        # Font objects should be different (different pixel sizes)
        if fonts_72 and fonts_120:
            assert fonts_72[0] is not fonts_120[0]
        _reset_cycle_cache()  # clean up


# -- font_family resolution ---------------------------------------------------


class TestFontFamilyResolution:
    """Tests for the font_family field — registry-based font resolution."""

    def test_font_family_loads_correct_font(self):
        """font_family='Space Grotesk' loads the correct .ttf."""
        font = _resolve_font_family("Space Grotesk", 72)
        assert font is not None

    def test_font_family_unknown_returns_none(self):
        """Unknown font_family returns None (falls through to font_style)."""
        font = _resolve_font_family("NonExistent Font 42", 72)
        assert font is None

    def test_font_family_renders_png(self, tmp_path):
        """Overlay with font_family set produces a valid PNG."""
        from PIL import Image

        result = generate_text_overlay_png(
            [{"text": "Hello", "start_s": 0.0, "end_s": 3.0,
              "position": "center", "effect": "none",
              "font_family": "Space Grotesk"}],
            5.0, str(tmp_path), 0,
        )
        assert result is not None
        img = Image.open(result[0]["png_path"])
        assert img.mode == "RGBA"
        assert img.size == (1080, 1920)
        alpha = img.split()[3]
        assert alpha.getextrema()[1] > 0

    def test_font_family_missing_falls_back_to_font_style(self, tmp_path):
        """Unknown font_family falls back to font_style rendering."""
        result = generate_text_overlay_png(
            [{"text": "Fallback", "start_s": 0.0, "end_s": 3.0,
              "position": "center", "effect": "none",
              "font_family": "NonExistent Font",
              "font_style": "sans"}],
            5.0, str(tmp_path), 0,
        )
        assert result is not None
        assert os.path.exists(result[0]["png_path"])

    def test_no_font_family_renders_identically_to_legacy(self, tmp_path):
        """REGRESSION: overlay without font_family uses font_style (same as before)."""
        result = generate_text_overlay_png(
            [{"text": "Legacy", "start_s": 0.0, "end_s": 3.0,
              "position": "center", "effect": "none",
              "font_style": "display", "text_size": "large"}],
            5.0, str(tmp_path), 0,
        )
        assert result is not None
        assert os.path.exists(result[0]["png_path"])

    def test_font_cycle_with_font_family_settle(self, tmp_path):
        """font_family set on font-cycle overlay becomes the settle font."""
        _reset_cycle_cache()
        result = generate_text_overlay_png(
            [{"text": "PERU", "start_s": 0.0, "end_s": 2.0,
              "position": "center", "effect": "font-cycle",
              "font_family": "Space Grotesk"}],
            5.0, str(tmp_path), 0,
        )
        assert result is not None
        # Should have cycling + settle frames
        assert len(result) > 3
        # Settle PNG should exist
        settle_pngs = [r for r in result if "settle" in r["png_path"]]
        assert len(settle_pngs) == 1
        _reset_cycle_cache()

    def test_cycle_cache_keyed_by_settle_font(self):
        """Different font_family values get different cycle font lists."""
        _reset_cycle_cache()
        fonts_default = _resolve_cycle_fonts(72, settle_font_name="_default")
        fonts_grotesk = _resolve_cycle_fonts(72, settle_font_name="Space Grotesk")
        # The settle font (index 0) should be different
        assert fonts_default[0] is not fonts_grotesk[0]
        _reset_cycle_cache()

    def test_ass_dynamic_fontname(self):
        """ASS header uses font_family's ass_name when font_family is set."""
        header = _build_ass_header("Space Grotesk")
        assert "Space Grotesk" in header
        assert "Playfair Display" not in header

    def test_ass_animated_with_font_family(self):
        """Animated overlay ASS uses font_family's ass_name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_animated_overlay_ass(
                [{"text": "Fade", "start_s": 0.0, "end_s": 3.0,
                  "position": "center", "effect": "fade-in",
                  "font_family": "Bodoni Moda"}],
                5.0, tmpdir, 0,
            )
            assert result is not None
            with open(result[0]) as f:
                content = f.read()
            assert "Bodoni Moda" in content

    def test_ass_without_font_family_uses_playfair(self):
        """ASS without font_family defaults to Playfair Display."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_animated_overlay_ass(
                [{"text": "Default", "start_s": 0.0, "end_s": 3.0,
                  "position": "center", "effect": "fade-in"}],
                5.0, tmpdir, 0,
            )
            assert result is not None
            with open(result[0]) as f:
                content = f.read()
            assert "Playfair Display" in content


# -- Text span rendering -----------------------------------------------------


def _make_span_overlay(
    spans: list[dict],
    text: str = "Welcome to PERU",
    start_s: float = 0.0,
    end_s: float = 3.0,
    position: str = "center",
    effect: str = "none",
    **kwargs,
) -> dict:
    return {
        "text": text,
        "start_s": start_s,
        "end_s": end_s,
        "position": position,
        "effect": effect,
        "spans": spans,
        **kwargs,
    }


class TestSpanRendering:
    """Tests for _draw_spans_png and span-aware font-cycle."""

    def test_draw_spans_two_fonts_two_colors(self, tmp_path):
        """Two spans with different fonts and colors produce a valid PNG."""
        from PIL import Image

        overlay = _make_span_overlay(
            spans=[
                {"text": "Welcome to", "font_family": "Montserrat", "text_color": "#FFFFFF"},
                {"text": "PERU", "font_family": "Playfair Display", "text_color": "#F4D03F"},
            ],
        )
        result = generate_text_overlay_png(
            [overlay], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        assert len(result) == 1
        img = Image.open(result[0]["png_path"])
        assert img.mode == "RGBA"
        assert img.size == (1080, 1920)
        alpha = img.split()[3]
        assert alpha.getextrema()[1] > 0  # has visible text

    def test_draw_spans_single_span_matches_flat(self, tmp_path):
        """A single span should produce a PNG (not crash)."""
        overlay = _make_span_overlay(
            spans=[{"text": "Hello"}],
            text="Hello",
        )
        result = generate_text_overlay_png(
            [overlay], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        assert os.path.exists(result[0]["png_path"])

    def test_draw_spans_baseline_alignment(self, tmp_path):
        """Spans with different sizes both render (baseline-aligned)."""
        from PIL import Image

        overlay = _make_span_overlay(
            spans=[
                {"text": "small", "text_size": "small"},
                {"text": "LARGE", "text_size": "xlarge"},
            ],
        )
        result = generate_text_overlay_png(
            [overlay], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        img = Image.open(result[0]["png_path"])
        alpha = img.split()[3]
        assert alpha.getextrema()[1] > 0

    def test_draw_spans_multiline_wrap(self, tmp_path):
        """Many spans exceeding canvas width wrap to multiple lines."""
        from PIL import Image

        # Create enough spans to exceed 90% of 1080px
        overlay = _make_span_overlay(
            spans=[{"text": "LONGWORD" * 3, "text_size": "large"} for _ in range(4)],
            text="test",
        )
        result = generate_text_overlay_png(
            [overlay], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        img = Image.open(result[0]["png_path"])
        assert img.mode == "RGBA"
        alpha = img.split()[3]
        assert alpha.getextrema()[1] > 0

    def test_draw_spans_overflow_scaledown(self, tmp_path):
        """A single span wider than the canvas gets scaled down to fit."""
        overlay = _make_span_overlay(
            spans=[{"text": "A" * 80, "text_size": "xlarge"}],
            text="test",
        )
        result = generate_text_overlay_png(
            [overlay], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        assert os.path.exists(result[0]["png_path"])

    def test_draw_spans_empty_falls_back(self, tmp_path):
        """Empty spans array falls back to flat text rendering."""
        overlay = {
            "text": "Flat text",
            "start_s": 0.0,
            "end_s": 3.0,
            "position": "center",
            "effect": "none",
            "spans": [],
        }
        result = generate_text_overlay_png(
            [overlay], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        assert os.path.exists(result[0]["png_path"])

    def test_draw_spans_inherits_overlay_defaults(self, tmp_path):
        """Spans without font_family/text_color inherit from overlay."""
        from PIL import Image

        overlay = _make_span_overlay(
            spans=[{"text": "Hello"}, {"text": "World"}],
            text_color="#FF0000",
            font_family="Montserrat",
        )
        result = generate_text_overlay_png(
            [overlay], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        img = Image.open(result[0]["png_path"])
        pixels = list(img.getdata())
        visible = [p for p in pixels if p[3] > 100]
        # Should have red pixels (inherited from overlay text_color)
        red_pixels = [p for p in visible if p[0] > 200 and p[1] < 50 and p[2] < 50]
        assert len(red_pixels) > 0, "Spans should inherit overlay text_color"

    def test_font_cycle_with_spans_fixed_and_cycling(self, tmp_path):
        """Font-cycle with spans: fixed-font spans stay, others cycle."""
        _reset_cycle_cache()
        overlay = _make_span_overlay(
            spans=[
                {"text": "Welcome to", "font_family": "Montserrat"},  # fixed
                {"text": "PERU"},  # cycles (no font_family)
            ],
            effect="font-cycle",
            end_s=2.0,
        )
        result = generate_text_overlay_png(
            [overlay], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        # Should produce multiple font-cycle frames
        assert len(result) > 3
        for r in result:
            assert os.path.exists(r["png_path"])
        _reset_cycle_cache()

    def test_font_cycle_spans_settle_phase(self, tmp_path):
        """Font-cycle with spans has a settle phase (no accel)."""
        _reset_cycle_cache()
        overlay = _make_span_overlay(
            spans=[
                {"text": "Welcome to", "font_family": "Montserrat"},
                {"text": "PERU"},
            ],
            effect="font-cycle",
            end_s=2.0,
        )
        result = generate_text_overlay_png(
            [overlay], slot_duration_s=5.0,
            output_dir=str(tmp_path), slot_index=0,
        )
        assert result is not None
        settle_pngs = [r for r in result if "_settle" in os.path.basename(r["png_path"])]
        assert len(settle_pngs) == 1, "Should have exactly one settle frame"
        assert result[-1]["end_s"] == 2.0
        _reset_cycle_cache()

    def test_validate_overlay_skips_truncation_with_spans(self):
        """_validate_overlay skips 40-char truncation when spans are present."""
        overlay = {
            "text": "A" * 60,
            "start_s": 0.0,
            "end_s": 3.0,
            "position": "center",
            "spans": [{"text": "short"}, {"text": "words"}],
        }
        text, start_s, end_s, position = _validate_overlay(overlay, 5.0)
        assert text is not None
        assert len(text) == 60  # NOT truncated
        assert "\u2026" not in text
