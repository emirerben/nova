"""Tests for text_overlay.py — PNG rendering and ASS animated effects."""

import os
import tempfile

import pytest

from app.pipeline.text_overlay import (
    ASS_ANIMATED_EFFECTS,
    generate_animated_overlay_ass,
    generate_text_overlay_png,
    _validate_overlay,
    _write_animated_ass,
    _validate_ass_file,
    CANVAS_H,
    CANVAS_W,
    MAX_OVERLAY_TEXT_LEN,
)


# ── _validate_overlay ────────────────────────────────────────────────────────


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


# ── ASS animated overlay generation ──────────────────────────────────────────


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


# ── PNG overlay generation ───────────────────────────────────────────────────


class TestPNGOverlay:
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
