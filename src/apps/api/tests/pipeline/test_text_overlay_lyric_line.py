"""ASS rendering tests for the `lyric-line` effect.

Companion to `test_text_overlay_karaoke.py`. The lyric-line effect is the
plain YouTube-lyric-video style: one static block of text with eased
ASS alpha transforms, no per-word color sweep.
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


def _style_fields(content: str) -> list[str]:
    style_line = next(line for line in content.splitlines() if line.startswith("Style: LyricLine,"))
    return style_line.removeprefix("Style: ").split(",")


def test_lyric_line_emits_plain_text_with_alpha_transforms() -> None:
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
    assert r"\alpha&HFF&" in content
    assert r"\t(0,150,0.5,\alpha&H00&)" in content
    assert r"\t(1750,2000,2.0,\alpha&HFF&)" in content
    assert "\\fad(" not in content
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
    assert r"\t(0,300,0.5,\alpha&H00&)" in content
    assert r"\t(1400,2000,2.0,\alpha&HFF&)" in content


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


def test_lyric_line_style_has_outline_and_shadow() -> None:
    content = _render(
        {
            "effect": "lyric-line",
            "text": "Styled",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "bottom",
            "font_family": "Inter Tight",
            "fade_in_ms": 150,
            "fade_out_ms": 250,
        }
    )
    fields = _style_fields(content)
    assert fields[0] == "LyricLine"
    assert fields[2] == "90"
    assert fields[5] == "&H00000000"
    assert fields[6].startswith("&H99")
    assert fields[7] == "0"
    assert fields[15] == "1"
    assert fields[16] == "1.5"
    assert fields[17] == "2"


def test_zero_fade_out_emits_no_fade_out_transform() -> None:
    content = _render(
        {
            "effect": "lyric-line",
            "text": "Hold",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "bottom",
            "fade_in_ms": 150,
            "fade_out_ms": 0,
        }
    )
    assert content.count(r"\t(") == 1


# -- Wrap + shrink (fit-to-screen) --------------------------------------------
# The Pillow + libass lyric-line branch uses \q2, so libass never auto-wraps.
# Without explicit \N breaks, a long lyric line ("I hope I make it outta here
# (let's go! Yeah)") rendered at the LyricLine Style's Fontsize=90 overflows
# past the 1080px frame edge — see job 09a2afa1's "Highest in the Room"
# output. These tests lock in the wrap + shrink behavior added in
# text_overlay._wrap_and_shrink_for_lyric_line.


def _dialogue_line(content: str) -> str:
    return next(line for line in content.splitlines() if line.startswith("Dialogue:"))


def test_lyric_line_long_text_inserts_newlines() -> None:
    overlay = {
        "effect": "lyric-line",
        "text": "I hope I make it outta here (let's go! Yeah)",
        "start_s": 0.0,
        "end_s": 3.0,
        "position": "bottom",
        "font_family": "Inter Tight",
        "fade_in_ms": 150,
        "fade_out_ms": 250,
    }
    dialogue = _dialogue_line(_render(overlay))
    assert "\\N" in dialogue, "long lyric line must be wrapped with \\N breaks"


def test_lyric_line_extreme_length_emits_fs_shrink() -> None:
    # ~165 chars — too wide to fit even after wrapping at Fontsize=90, so the
    # shrink loop should drop the font below 90.
    overlay = {
        "effect": "lyric-line",
        "text": (
            "Supercalifragilisticexpialidociouslyverylongwordthatshouldnotbreak "
            "and then a tail with more words still going on past the line cap"
        ),
        "start_s": 0.0,
        "end_s": 3.0,
        "position": "bottom",
        "font_family": "Inter Tight",
        "fade_in_ms": 150,
        "fade_out_ms": 250,
    }
    dialogue = _dialogue_line(_render(overlay))
    # \fs<n> must appear with n strictly less than the LyricLine style's 90.
    import re

    matches = re.findall(r"\\fs(\d+)", dialogue)
    assert matches, "expected a \\fs override on extreme-length lyric line"
    assert all(int(n) < 90 for n in matches), f"\\fs sizes {matches} should all be < 90"


def test_lyric_line_short_text_no_wrap_no_shrink() -> None:
    overlay = {
        "effect": "lyric-line",
        "text": "OK",
        "start_s": 0.0,
        "end_s": 2.0,
        "position": "bottom",
        "font_family": "Inter Tight",
        "fade_in_ms": 150,
        "fade_out_ms": 250,
    }
    dialogue = _dialogue_line(_render(overlay))
    assert "\\N" not in dialogue, "short lyric must not gain spurious \\N breaks"
    assert "\\fs" not in dialogue, "short lyric must not gain a \\fs override"


def test_lyric_line_text_size_px_emits_fs_tag() -> None:
    # The injector sets text_size_px=56 by default; the renderer must emit
    # \fs56 to override the LyricLine Style's Fontsize=90.
    overlay = {
        "effect": "lyric-line",
        "text": "Steady",
        "start_s": 0.0,
        "end_s": 2.0,
        "position": "bottom",
        "font_family": "Inter Tight",
        "fade_in_ms": 150,
        "fade_out_ms": 250,
        "text_size_px": 56,
    }
    dialogue = _dialogue_line(_render(overlay))
    assert "\\fs56" in dialogue
