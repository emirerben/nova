"""Karaoke-line ASS rendering tests."""

from __future__ import annotations

import os
import tempfile

from app.pipeline.text_overlay import (
    ASS_ANIMATED_EFFECTS,
    _hex_to_ass_bgr,
    _wrap_karaoke_timed_words_for_ass,
    generate_animated_overlay_ass,
)


def test_karaoke_line_is_registered_as_animated_effect() -> None:
    assert "karaoke-line" in ASS_ANIMATED_EFFECTS


def test_hex_to_ass_bgr_swaps_bytes() -> None:
    assert _hex_to_ass_bgr("#FFFFFF") == "FFFFFF"
    assert _hex_to_ass_bgr("#FF0000") == "0000FF"  # red -> BGR
    assert _hex_to_ass_bgr("#00FF00") == "00FF00"
    assert _hex_to_ass_bgr("#0000FF") == "FF0000"
    assert _hex_to_ass_bgr("invalid") == "FFFFFF"


def test_karaoke_line_renders_ass_file_with_start_anchored_sung_words() -> None:
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
        assert r"\kt" not in content
        assert r"\kf" not in content
        dialogue_lines = [line for line in content.splitlines() if line.startswith("Dialogue:")]
        assert len(dialogue_lines) == 2
        assert "0:00:00.00,0:00:00.40" in dialogue_lines[0]
        assert "0:00:00.40,0:00:01.00" in dialogue_lines[1]
        assert "Hello" in content
        assert "world" in content
        # ASS color encoding is BGR: yellow active word, white inactive word.
        assert r"\1c&H00FFFF&}Hello" in dialogue_lines[0]
        assert r"\1c&HFFFFFF&}world" in dialogue_lines[0]
        assert r"\1c&H00FFFF&}Hello" in dialogue_lines[1]
        assert r"\1c&H00FFFF&}world" in dialogue_lines[1]


def test_karaoke_line_anchors_word_starts_and_preserves_gaps() -> None:
    """Explicit start_s/end_s must survive into ASS so audio gaps do not drift."""
    overlays = [
        {
            "effect": "karaoke-line",
            "text": "wait now",
            "start_s": 0.0,
            "end_s": 1.5,
            "position": "bottom",
            "text_color": "#FFFFFF",
            "highlight_color": "#FFFF00",
            "word_timings": [
                {"text": "wait", "start_s": 0.0, "end_s": 0.3, "duration_cs": 30},
                {"text": "now", "start_s": 1.0, "end_s": 1.3, "duration_cs": 100},
            ],
        }
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        ass_paths = generate_animated_overlay_ass(
            overlays, slot_duration_s=2.0, output_dir=tmpdir, slot_index=0
        )
        assert ass_paths is not None
        content = open(ass_paths[0]).read()  # noqa: PTH123, SIM115
        assert r"\kt" not in content
        assert r"\kf" not in content
        dialogue_lines = [line for line in content.splitlines() if line.startswith("Dialogue:")]
        assert len(dialogue_lines) == 2
        assert "0:00:00.00,0:00:01.00" in dialogue_lines[0]
        assert r"\1c&H00FFFF&}wait" in dialogue_lines[0]
        assert r"\1c&HFFFFFF&}now" in dialogue_lines[0]
        assert "0:00:01.00,0:00:01.50" in dialogue_lines[1]
        assert r"\1c&H00FFFF&}wait" in dialogue_lines[1]
        assert r"\1c&H00FFFF&}now" in dialogue_lines[1]


def test_karaoke_ass_highlight_switches_at_onset_and_keeps_sung_words() -> None:
    """ASS preview should switch at word onset and keep completed words yellow."""
    overlays = [
        {
            "effect": "karaoke-line",
            "text": "What comes next",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "bottom",
            "text_color": "#FFFFFF",
            "highlight_color": "#FFFF00",
            "word_timings": [
                {"text": "What", "start_s": 0.0, "end_s": 0.25, "duration_cs": 25},
                {"text": "comes", "start_s": 0.25, "end_s": 0.45, "duration_cs": 20},
                {"text": "next", "start_s": 0.45, "end_s": 0.95, "duration_cs": 50},
            ],
        }
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        ass_paths = generate_animated_overlay_ass(
            overlays, slot_duration_s=2.0, output_dir=tmpdir, slot_index=0
        )
        assert ass_paths is not None
        content = open(ass_paths[0]).read()  # noqa: PTH123, SIM115

    assert r"\kt" not in content
    assert r"\kf" not in content
    dialogue_lines = [line for line in content.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogue_lines) == 3
    assert "0:00:00.00,0:00:00.25" in dialogue_lines[0]
    assert r"\1c&H00FFFF&}What" in dialogue_lines[0]
    assert r"\1c&HFFFFFF&}comes" in dialogue_lines[0]
    assert r"\1c&HFFFFFF&}next" in dialogue_lines[0]
    assert "0:00:00.25,0:00:00.45" in dialogue_lines[1]
    assert r"\1c&H00FFFF&}What" in dialogue_lines[1]
    assert r"\1c&H00FFFF&}comes" in dialogue_lines[1]
    assert r"\1c&HFFFFFF&}next" in dialogue_lines[1]
    assert "0:00:00.45,0:00:02.00" in dialogue_lines[2]
    assert r"\1c&H00FFFF&}What" in dialogue_lines[2]
    assert r"\1c&H00FFFF&}comes" in dialogue_lines[2]
    assert r"\1c&H00FFFF&}next" in dialogue_lines[2]


def test_karaoke_line_wraps_long_text_without_shrinking_ass_font() -> None:
    words = "Let's make this happen let's make this happen".split()
    overlays = [
        {
            "effect": "karaoke-line",
            "text": " ".join(words),
            "start_s": 0.0,
            "end_s": 3.0,
            "position": "bottom",
            "font_family": "Inter",
            "text_size_px": 120,
            "text_color": "#FFFFFF",
            "highlight_color": "#FFFF00",
            "word_timings": [
                {
                    "text": word,
                    "start_s": i * 0.25,
                    "end_s": i * 0.25 + 0.2,
                    "duration_cs": 20,
                }
                for i, word in enumerate(words)
            ],
        }
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        ass_paths = generate_animated_overlay_ass(
            overlays, slot_duration_s=3.0, output_dir=tmpdir, slot_index=0
        )
        assert ass_paths is not None
        content = open(ass_paths[0]).read()  # noqa: PTH123, SIM115
        assert r"\N" in content
        assert r"\fs120" in content
        assert r"\fs102" not in content


def test_karaoke_ass_wrap_balances_production_orphan_lines() -> None:
    text = "I only call you when it's half past five"
    timed_words = [{"text": word, "duration_cs": 20} for word in text.split()]

    lines = _wrap_karaoke_timed_words_for_ass(
        timed_words,
        font_family="Bodoni Moda",
        base_size_px=64,
    )

    counts = [len(line) for line in lines]
    assert len(lines) == 2
    assert counts == [5, 4]


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
        assert r"\kt" not in content


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
