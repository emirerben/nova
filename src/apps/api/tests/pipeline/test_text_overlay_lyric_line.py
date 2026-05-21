"""ASS rendering tests for the `lyric-line` effect.

Companion to `test_text_overlay_karaoke.py`. The lyric-line effect is the
plain YouTube-lyric-video style: one static block of text with a smooth
`\\fad(in, out)` alpha animation, no per-word color sweep.
"""

from __future__ import annotations

import tempfile

from app.pipeline.text_overlay import ASS_ANIMATED_EFFECTS, generate_animated_overlay_ass


def test_lyric_line_is_registered_as_animated_effect() -> None:
    assert "lyric-line" in ASS_ANIMATED_EFFECTS


def _render(overlay: dict, slot_duration_s: float = 5.0) -> str:
    """Render one overlay and return the ASS file's text."""
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = generate_animated_overlay_ass(
            [overlay], slot_duration_s=slot_duration_s, output_dir=tmpdir, slot_index=0
        )
        assert paths and len(paths) == 1
        with open(paths[0], encoding="utf-8") as f:
            return f.read()


def test_lyric_line_emits_plain_text_with_fad_tag() -> None:
    overlay = {
        "effect": "lyric-line",
        "text": "Hello world",
        "start_s": 0.5,
        "end_s": 2.5,
        "position": "bottom",
        "text_color": "#FFFFFF",
        "fade_in_ms": 150,
        "fade_out_ms": 250,
    }
    content = _render(overlay)
    # Fade tag present with the supplied durations.
    assert "\\fad(150,250)" in content
    # No per-word color sweep tag.
    assert "\\kf" not in content
    # No karaoke secondary-color setup.
    assert "\\2c" not in content
    # The plain text is in the dialogue.
    assert "Hello world" in content


def test_lyric_line_uses_overlay_fade_overrides() -> None:
    overlay = {
        "effect": "lyric-line",
        "text": "Tuned",
        "start_s": 0.0,
        "end_s": 2.0,
        "position": "bottom",
        "text_color": "#FFFFFF",
        "fade_in_ms": 300,
        "fade_out_ms": 600,
    }
    content = _render(overlay)
    assert "\\fad(300,600)" in content


def test_lyric_line_applies_text_color() -> None:
    overlay = {
        "effect": "lyric-line",
        "text": "Red line",
        "start_s": 0.0,
        "end_s": 2.0,
        "position": "bottom",
        "text_color": "#FF0000",  # red → BGR 0000FF
        "fade_in_ms": 150,
        "fade_out_ms": 250,
    }
    content = _render(overlay)
    assert "\\1c&H0000FF&" in content


def test_lyric_line_position_alignment_for_bottom() -> None:
    overlay = {
        "effect": "lyric-line",
        "text": "Anchored",
        "start_s": 0.0,
        "end_s": 2.0,
        "position": "bottom",
        "text_color": "#FFFFFF",
        "fade_in_ms": 150,
        "fade_out_ms": 250,
    }
    content = _render(overlay)
    # Bottom anchor is \an2 (per _ASS_POSITION in text_overlay.py).
    assert "\\an2" in content
