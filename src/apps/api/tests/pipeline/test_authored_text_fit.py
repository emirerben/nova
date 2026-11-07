"""Authored (`wrap_lines=false`) text fit across renderers (KRI-885, KRI-886, KRI-887).

Failure modes this file pins:

* KRI-885: a legacy line dragged off-centre that fits the frame is re-wrapped because the
  room was measured to the 5% safe margins instead of the frame edge; a line that really
  runs past the frame edge must still wrap.
* KRI-886: a run with no spaces (emoji chain, CJK, URL, combining marks) overflows at the
  shrink floor and is clipped; a break inside it must never split a grapheme cluster
  (ZWJ family, flag, skin tone, base + combining mark), must happen only at the floor, and
  a cluster wider than the box must be kept whole and reported, never dropped.
* KRI-887: `measure_text_block` wraps `wrap_lines=false` rows at 0.84 x W while the burn
  wraps at the authored fit width, so a stack reserves the wrong number of rows.
"""

import numpy as np
import pytest
import regex
import skia
import structlog
from PIL import Image, ImageDraw

from app.pipeline import text_overlay as pillow
from app.pipeline import text_overlay_skia as cloud
from app.pipeline.canvas import Canvas
from app.pipeline.text_fit import authored_safe_width
from app.pipeline.text_stack_layout import measure_text_block

W = 1080
FRAME = Canvas(W, 1920)
SENTENCE = "Kadıköy'de sabahları çay içiyoruz"


def _burn(text, x_frac=0.5, anchor="center", size=72, family="Inter"):
    return {
        "text": text,
        "font_family": family,
        "text_size_px": size,
        "position_x_frac": x_frac,
        "text_anchor": anchor,
        "wrap_lines": False,
        "max_width_frac": 0.3,
    }


def _skia_rows(overlay, canvas=FRAME):
    face = cloud._typeface_for_overlay(overlay)
    shaped = cloud._auto_shape(face, overlay["text"], False)
    font, size, rows = cloud._authored_lines(
        overlay["text"],
        face,
        overlay["text_size_px"],
        cloud._overlay_max_width_px(overlay, canvas),
        shape_text=shaped,
    )
    return font, size, rows, shaped


def _width(text, size, family="Inter"):
    face = cloud._typeface_for_overlay(_burn(text, family=family))
    return cloud._measure_line(skia.Font(face, size), text)


def _size_for_width(text, low, high):
    """The integer size at which ``text`` is between ``low`` and ``high`` px wide."""
    for size in range(30, 200):
        if low <= _width(text, size) <= high:
            return size
    raise AssertionError((text, low, high))


# -- KRI-885 -----------------------------------------------------------------------------


def _x_with_frame_room(width, anchor, slack=4):
    """Anchor x (fraction) at which ``width`` just fits between the anchor and the frame edge."""
    room = width + slack
    guard = 0.03 * W
    cx = {"center": W - guard - room / 2, "left": W - guard - room, "right": room + guard}[anchor]
    return cx / W


@pytest.mark.parametrize("anchor", ["center", "left", "right"])
def test_legacy_line_that_fits_the_frame_is_not_rewrapped(anchor):
    text = "Kadıköy'de sabahları"
    width = _width(text, 72)
    x_frac = _x_with_frame_room(width, anchor)
    old_room = {"center": 2 * (0.95 - x_frac) * W, "left": 0.95 * W - x_frac * W}.get(
        anchor, x_frac * W - 0.05 * W
    )
    assert old_room < width, "precondition: the margin-based room would have wrapped it"
    _font, size, rows, _ = _skia_rows(_burn(text, x_frac, anchor))
    assert rows == [text] and size == 72


@pytest.mark.parametrize(
    ("x_frac", "anchor"), [(0.7, "center"), (0.3, "left"), (0.8, "right"), (0.15, "center")]
)
def test_line_past_the_frame_edge_still_wraps_inside_the_frame(x_frac, anchor):
    text = " ".join([SENTENCE] * 3)
    overlay = _burn(text, x_frac, anchor)
    font, _size, rows, shaped = _skia_rows(overlay)
    assert len(rows) > 1
    cx = x_frac * W
    for row in rows:
        width = cloud._measure_line(font, row, shape_text=shaped)
        left = {"left": cx, "right": cx - width}.get(anchor, cx - width / 2)
        assert left >= -0.5 and left + width <= W + 0.5, (row, left, width)


def test_pillow_lane_uses_the_same_room_as_skia():
    text = "Kadıköy'de sabahları"
    draw = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    box = draw.textbbox((0, 0), text, font=pillow._resolve_font_family("Inter", 72))
    x_frac = _x_with_frame_room(box[2] - box[0], "center")
    _font, size, rows = pillow._authored_rows_pillow(
        text,
        lambda px: pillow._resolve_font_family("Inter", px),
        72,
        authored_safe_width(W, x_frac, "center"),
        draw,
    )
    assert rows == [text] and size == 72


# -- KRI-886 -----------------------------------------------------------------------------

UNBREAKABLE = {
    "emoji_chain": "🔥" * 90,
    "zwj_families": "👨‍👩‍👧‍👦" * 120,
    "flags": "🇹🇷🇯🇵🇧🇷" * 30,
    "skin_tones": "👍🏽👍🏿" * 40,
    "combining": "éä" * 90,
    "cjk": "今日は天気がいいので散歩に行きましょう" * 9,
    "hangul": "오늘은날씨가좋아서산책을하러갑니다" * 10,
    "url": "https://example.com/" + "a-very-long-path-segment/" * 12,
}


def _graphemes(text):
    return regex.findall(r"\X", text)


@pytest.mark.parametrize("name", UNBREAKABLE)
def test_unbreakable_run_breaks_only_at_the_floor_and_never_inside_a_cluster(name):
    text = UNBREAKABLE[name]
    overlay = _burn(text)
    font, size, rows, shaped = _skia_rows(overlay)
    max_width = cloud._overlay_max_width_px(overlay, FRAME)
    assert size >= 24
    assert "".join(rows) == text
    assert sum(len(_graphemes(row)) for row in rows) == len(_graphemes(text))
    assert [g for row in rows for g in _graphemes(row)] == _graphemes(text), (
        "a row boundary split a cluster"
    )
    assert len(rows) > 1 and size == 24, (
        "the run must overflow the box at the floor so it is broken"
    )
    widest = max(cloud._measure_line(font, row, shape_text=shaped) for row in rows)
    assert widest <= max_width + 0.5, (name, widest, max_width)


def test_a_run_that_fits_after_shrinking_is_not_broken():
    text = "今日は天気がいいので散歩に行きましょう"
    overlay = _burn(text, size=96)
    _font, size, rows, _ = _skia_rows(overlay)
    assert rows == [text] and 24 <= size < 96


def test_a_cluster_wider_than_the_box_is_kept_whole_and_reported():
    text = "A👨‍👩‍👧‍👦B"
    face = cloud._typeface_for_overlay(_burn(text))
    with structlog.testing.capture_logs() as logs:
        _font, size, rows = cloud._authored_lines(text, face, 72, 4.0, shape_text=True)
    assert "".join(rows) == text
    assert "👨‍👩‍👧‍👦" in rows and size == 24
    assert any(entry["event"] == "authored_text_overflow" for entry in logs)


def test_fitting_text_reports_no_overflow():
    face = cloud._typeface_for_overlay(_burn("x"))
    with structlog.testing.capture_logs() as logs:
        cloud._authored_lines("Short line", face, 72, 972.0)
    assert not [entry for entry in logs if entry["event"] == "authored_text_overflow"]


@pytest.mark.parametrize("name", ["cjk", "hangul", "url", "combining"])
def test_pillow_lane_breaks_the_same_way(name):
    text = UNBREAKABLE[name]
    draw = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    family = pillow.glyph_safe_family("Inter", text) or "Inter"
    max_width = authored_safe_width(W, 0.5, "center")
    font, size, rows = pillow._authored_rows_pillow(
        text, lambda px: pillow._resolve_font_family(family, px), 72, max_width, draw
    )
    assert "".join(rows) == text and size >= 24
    assert sum(len(_graphemes(row)) for row in rows) == len(_graphemes(text))
    box = [draw.textbbox((0, 0), row, font=font) for row in rows]
    assert max(b[2] - b[0] for b in box) <= max_width
    if len(rows) > 1:
        assert size == 24


def test_words_with_spaces_still_wrap_at_word_boundaries():
    text = " ".join([SENTENCE] * 3)
    _font, size, rows, _ = _skia_rows(_burn(text))
    assert size == 72 and " ".join(rows).split() == text.split()


# -- KRI-887 -----------------------------------------------------------------------------


def _stack_row(text, **extra):
    return {
        "id": "pin-0",
        "text": text,
        "start_s": 0,
        "end_s": 4,
        "position": "custom",
        "x_frac": 0.5,
        "y_frac": 0.5,
        "alignment": "center",
        "font_family": "Inter",
        "size_px": 72,
        "wrap_lines": False,
        "max_width_frac": 0.84,
        **extra,
    }


@pytest.mark.parametrize("alignment", ["center", "left", "right"])
def test_measure_text_block_wraps_like_the_burn(alignment):
    text = "Kadıköy'de sabahları çay içiyoruz"
    size = _size_for_width(text, 0.86 * W, 0.89 * W)
    assert _width(text, size) > 0.84 * W
    x_frac = _x_with_frame_room(_width(text, size), alignment)
    row = _stack_row(text, x_frac=x_frac, alignment=alignment, size_px=size)
    burn_rows = _skia_rows(_burn(text, x_frac, alignment, size))[2]
    assert burn_rows == [text]
    assert measure_text_block(row)["lines"] == burn_rows


def test_measure_text_block_uses_the_given_canvas_width():
    text = "Kadıköy'de sabahları çay içiyoruz"
    size = _size_for_width(text, 0.86 * 600, 0.88 * 600)
    row = _stack_row(text, size_px=size)
    burn_rows = _skia_rows(_burn(text, size=size), Canvas(600, 800))[2]
    assert measure_text_block(row, width=600)["lines"] == burn_rows == [text]


def test_measure_text_block_keeps_wrapping_rows_on_their_box():
    text = "Kadıköy'de sabahları çay içiyoruz"
    size = _size_for_width(text, 0.87 * W, 0.89 * W)
    assert len(measure_text_block(_stack_row(text, wrap_lines=True, size_px=size))["lines"]) > 1


# -- cluster-broken rows under a reveal --------------------------------------------------


def _drawn_text(overlay, t_local, duration_s=4.0):
    from unittest.mock import patch

    with patch.object(cloud, "_draw_line_with_layers") as draw:
        cloud._draw_overlay_on_canvas(
            skia.Surface(W, 1920).getCanvas(), overlay, t_local, duration_s, render_canvas=FRAME
        )
    return "".join(call.args[1] for call in draw.call_args_list)


def _reveal_case(name, **extra):
    text = UNBREAKABLE[name]
    return text, {**_burn(text), "start_s": 0, "end_s": 4, "shadow_enabled": False, **extra}


@pytest.mark.parametrize("name", ["emoji_chain", "cjk", "hangul", "url", "combining"])
def test_fixed_reveal_lines_keeps_every_character_of_cluster_broken_rows(name):
    text = UNBREAKABLE[name]
    _font, _size, rows, _ = _skia_rows(_burn(text))
    assert len(rows) > 1 and "".join(rows) == text
    revealed, cursor_line = cloud._fixed_reveal_lines(rows, text, text)
    assert revealed == rows and cursor_line == len(rows) - 1


def test_fixed_reveal_lines_still_skips_the_space_and_newline_between_rows():
    rows = ["alpha beta", "gamma", "delta"]
    full = "alpha beta gamma\ndelta"
    assert cloud._fixed_reveal_lines(rows, full, full)[0] == rows
    revealed, cursor_line = cloud._fixed_reveal_lines(rows, full[:13], full)
    assert revealed == ["alpha beta", "ga", ""] and cursor_line == 1


@pytest.mark.parametrize("name", ["emoji_chain", "cjk", "url", "combining"])
@pytest.mark.parametrize(
    "phases",
    [
        {"entrance": "none", "exit": "none"},
        {"entrance": "wordRelay", "exit": "none"},
        {"entrance": "silkReveal", "exit": "none"},
    ],
    ids=["plain_phases", "word_relay_hold", "designed_hold"],
)
def test_phase_animated_hold_draws_every_character(name, phases):
    text, overlay = _reveal_case(name, animation_phases=phases)
    assert _drawn_text(overlay, 2.0) == text


@pytest.mark.parametrize("effect", ["typewriter", "stream-in"])
@pytest.mark.parametrize("name", ["emoji_chain", "url"])
def test_typewriter_hold_draws_every_character(name, effect):
    text, overlay = _reveal_case(name, effect=effect, end_s=120)
    assert _drawn_text(overlay, 119.0, 120.0) == text


@pytest.mark.parametrize("name", ["emoji_chain", "cjk", "hangul", "url"])
def test_pillow_phase_hold_paints_every_character(name, tmp_path):
    text = UNBREAKABLE[name]
    overlay = {
        **_burn(text),
        "start_s": 0,
        "end_s": 4,
        "shadow_enabled": False,
        "animation_phases": {"entrance": "none", "exit": "none"},
    }
    held = tmp_path / "held.png"
    pillow._render_authored_phase_frame(overlay, text, "center", str(held), 2.0, 4)
    plain = tmp_path / "plain.png"
    pillow._render_static_overlay_layer(
        {k: v for k, v in overlay.items() if k != "animation_phases"}, text, "center", str(plain)
    )
    held_ink = int(np.asarray(Image.open(held).getchannel("A"), dtype=np.int64).sum())
    plain_ink = int(np.asarray(Image.open(plain).getchannel("A"), dtype=np.int64).sum())
    assert held_ink == pytest.approx(plain_ink, rel=0.002)
