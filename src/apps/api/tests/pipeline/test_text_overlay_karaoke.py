"""Karaoke-line ASS rendering tests."""

from __future__ import annotations

import os
import tempfile

from app.pipeline.text_overlay import (
    ASS_ANIMATED_EFFECTS,
    _hex_to_ass_bgr,
    generate_animated_overlay_ass,
)


def test_karaoke_line_is_registered_as_animated_effect() -> None:
    assert "karaoke-line" in ASS_ANIMATED_EFFECTS


def test_hex_to_ass_bgr_swaps_bytes() -> None:
    assert _hex_to_ass_bgr("#FFFFFF") == "FFFFFF"
    assert _hex_to_ass_bgr("#FF0000") == "0000FF"  # red → BGR
    assert _hex_to_ass_bgr("#00FF00") == "00FF00"
    assert _hex_to_ass_bgr("#0000FF") == "FF0000"
    assert _hex_to_ass_bgr("invalid") == "FFFFFF"


def test_karaoke_line_renders_ass_file_with_k_tags() -> None:
    overlays = [
        {
            "effect": "karaoke-line",
            "text": "Hello world",
            "start_s": 0.0,
            "end_s": 1.0,
            "position": "bottom",
            "text_color": "#FFFFFF",
            "highlight_color": "#FFFF00",
            "word_timings": [
                {"text": "Hello", "duration_cs": 40},
                {"text": "world", "duration_cs": 60},
            ],
        }
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        ass_paths = generate_animated_overlay_ass(
            overlays, slot_duration_s=2.0, output_dir=tmpdir, slot_index=0
        )
        assert ass_paths is not None
        assert len(ass_paths) == 1

        content = open(ass_paths[0]).read()  # noqa: PTH123, SIM115
        # Has karaoke tags
        assert r"\kf40" in content
        assert r"\kf60" in content
        assert "Hello" in content
        assert "world" in content
        # Primary / secondary colors emitted as BGR
        assert r"\1c&HFFFFFF&" in content  # white text_color
        assert r"\2c&H00FFFF&" in content  # yellow highlight (#FFFF00 → 00FFFF)


def test_karaoke_line_without_word_timings_falls_back_gracefully() -> None:
    """No word_timings → render plain text without karaoke tags; never crash."""
    overlays = [
        {
            "effect": "karaoke-line",
            "text": "Hello",
            "start_s": 0.0,
            "end_s": 1.0,
            "position": "bottom",
            "text_color": "#FFFFFF",
            "word_timings": [],
        }
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        ass_paths = generate_animated_overlay_ass(
            overlays, slot_duration_s=2.0, output_dir=tmpdir, slot_index=0
        )
        assert ass_paths is not None
        content = open(ass_paths[0]).read()  # noqa: PTH123, SIM115
        assert "Hello" in content
        assert r"\kf" not in content


def test_karaoke_overlay_path_exists_and_nonempty() -> None:
    overlays = [
        {
            "effect": "karaoke-line",
            "text": "Hi",
            "start_s": 0.0,
            "end_s": 0.5,
            "position": "bottom",
            "text_color": "#FFFFFF",
            "word_timings": [{"text": "Hi", "duration_cs": 50}],
        }
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = generate_animated_overlay_ass(overlays, 1.0, tmpdir, 0)
        assert paths is not None
        for p in paths:
            assert os.path.exists(p)
            assert os.path.getsize(p) > 0
