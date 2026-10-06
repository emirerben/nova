"""Real-render pixel tests for the slide-post text-parity style fields.

No ffmpeg needed: `render_text_element_png` rasters through Pillow only.
"""

from __future__ import annotations

import hashlib

import pytest
from PIL import Image

from app.pipeline.slide_post.build import (
    edits_cache_digest,
    render_text_element_png,
)
from app.schemas.slide_post import SlideEdits, SlideTextElement

C916 = (1080, 1920)
C45 = (1080, 1350)
CANVASES = [C916, C45]


def _render(tmp_path, canvas, **kw) -> Image.Image:
    base = {"id": "t", "text": "Hello world", "position": "center", "size_px": 120}
    base.update(kw)
    path = tmp_path / "t.png"
    render_text_element_png(SlideTextElement(**base), str(path), canvas=canvas)
    with Image.open(path) as im:
        out = im.convert("RGBA")
    assert out.size == canvas
    return out


def _opaque_bbox(img: Image.Image, min_alpha: int = 200):
    return img.getchannel("A").point(lambda a: 255 if a >= min_alpha else 0).getbbox()


def _count(img: Image.Image, pred) -> int:
    return sum(1 for r, g, b, a in img.getdata() if a > 0 and pred(r, g, b, a))


def _h(img: Image.Image) -> str:
    return hashlib.sha256(img.tobytes()).hexdigest()[:16]


# --- legacy byte-identity -------------------------------------------------------------
# Hashes were computed from the renderer on origin/main BEFORE the parity change
# (raw RGBA raster hash). An element using only pre-parity fields must not move.
# Re-pinned 2026-10-06 for the cases on the default face: `DEFAULT_SLIDE_TEXT_FONT`
# ("Inter-Bold") used to miss the registry and burn Playfair Display Bold; it now
# resolves to Inter Bold like the legacy drawtext path and the client previews
# (tests/pipeline/test_font_aliases.py). custom_box sets its face and is unchanged.
LEGACY_GOLDEN = {
    (1920, "default"): "de3a103332ebaee0",
    (1920, "top_left_stroke"): "bb4108ba7ed27e6b",
    (1920, "custom_box"): "12e288fcc5614eb8",
    (1920, "center_right"): "dced6cf557e4a64c",
    (1350, "default"): "4090836053087e06",
    (1350, "top_left_stroke"): "90edf92f2a7e18c8",
    (1350, "custom_box"): "4955f68d1cb9cd33",
    (1350, "center_right"): "fa48df085f3275cb",
}
LEGACY_CASES = {
    "default": dict(text="Hello world"),
    "top_left_stroke": dict(
        text="Stroke me please", position="top", alignment="left", stroke_width=6, color="#FFD400"
    ),
    "custom_box": dict(
        text="Boxed text that wraps over a couple of lines for sure",
        position="custom",
        x_frac=0.3,
        y_frac=0.4,
        background="box",
        shadow_enabled=False,
        max_width_frac=0.6,
        size_px=60,
        font_family="PlayfairDisplay-Bold",
    ),
    "center_right": dict(text="Right", position="center", alignment="right", size_px=120),
}


@pytest.mark.parametrize("canvas", CANVASES)
@pytest.mark.parametrize("name", list(LEGACY_CASES))
def test_legacy_only_elements_render_pixel_identical_to_pre_parity(tmp_path, canvas, name):
    kw = dict(LEGACY_CASES[name])
    path = tmp_path / "g.png"
    render_text_element_png(SlideTextElement(id="a", **kw), str(path), canvas=canvas)
    with Image.open(path) as im:
        assert _h(im.convert("RGBA")) == LEGACY_GOLDEN[(canvas[1], name)]


# --- new fields -----------------------------------------------------------------------


@pytest.mark.parametrize("canvas", CANVASES)
class TestParityRender:
    def test_rotation_turns_wide_text_tall(self, tmp_path, canvas):
        flat = _opaque_bbox(_render(tmp_path, canvas, text="WIDE TEXT"))
        turned = _opaque_bbox(_render(tmp_path, canvas, text="WIDE TEXT", rotation_deg=90))
        fw, fh = flat[2] - flat[0], flat[3] - flat[1]
        tw, th = turned[2] - turned[0], turned[3] - turned[1]
        assert fw > fh and th > tw
        assert abs(th - fw) < 0.15 * fw

    def test_rotation_zero_equals_unset(self, tmp_path, canvas):
        assert _h(_render(tmp_path, canvas, rotation_deg=0)) == _h(_render(tmp_path, canvas))

    def test_rotated_text_near_band_edge_is_cropped_not_dropped(self, tmp_path, canvas):
        # y_frac 0.02 sits at the very top of the canvas; a 45deg turn pushes
        # part of the glyphs above it. Rotation happens before the band crop,
        # so the visible remainder must still be there at the right size.
        img = _render(
            tmp_path,
            canvas,
            position="custom",
            y_frac=0.02,
            x_frac=0.5,
            rotation_deg=45,
            text="EDGE",
            size_px=160,
        )
        box = _opaque_bbox(img)
        assert box is not None and box[1] == 0

    def test_stroke_color_and_width(self, tmp_path, canvas):
        def green(img):
            return _count(img, lambda r, g, b, a: g > 200 and r < 80 and b < 80 and a > 200)

        plain = _render(tmp_path, canvas, stroke_width=8)
        colored = _render(tmp_path, canvas, stroke_width=8, stroke_color="#00FF00")
        assert green(plain) == 0 and green(colored) > 500
        # no width => color has nothing to paint
        assert green(_render(tmp_path, canvas, stroke_width=0, stroke_color="#00FF00")) == 0
        wide = _render(tmp_path, canvas, stroke_width=20, stroke_color="#00FF00")
        assert green(wide) > green(colored)

    def test_shadow_color_and_opacity(self, tmp_path, canvas):
        def red(img):
            return _count(img, lambda r, g, b, a: r > g + 80 and r > b + 80 and a > 20)

        loud = _render(tmp_path, canvas, shadow_color="#FF0000", shadow_opacity=1.0)
        quiet = _render(tmp_path, canvas, shadow_color="#FF0000", shadow_opacity=0.0)
        off = _render(
            tmp_path, canvas, shadow_color="#FF0000", shadow_opacity=1.0, shadow_enabled=False
        )
        assert red(loud) > 2000
        assert red(quiet) == 0 and red(off) == 0

    def test_highlight_background_color_paints_a_box(self, tmp_path, canvas):
        img = _render(
            tmp_path,
            canvas,
            background_color="#FFF0A6",
            editor_preset="Highlight",
            shadow_enabled=False,
        )
        box = _opaque_bbox(img)
        # a pixel in the box padding (inside bbox, left of the glyphs) is the highlight color
        x, y = box[0] + 3, (box[1] + box[3]) // 2
        assert img.getpixel((x, y)) == (255, 240, 166, 255)

    def test_background_color_wins_over_legacy_box(self, tmp_path, canvas):
        legacy = _render(tmp_path, canvas, background="box", shadow_enabled=False)
        both = _render(
            tmp_path, canvas, background="box", background_color="#FFF0A6", shadow_enabled=False
        )
        bx = _opaque_bbox(both, min_alpha=100)
        probe = (bx[0] + 3, (bx[1] + bx[3]) // 2)
        assert legacy.getpixel(probe)[3] < 200  # black @ .45 box
        assert both.getpixel(probe) == (255, 240, 166, 255)

    def test_text_case(self, tmp_path, canvas):
        upper = _render(tmp_path, canvas, text="hello there", text_case="upper")
        assert _h(upper) == _h(_render(tmp_path, canvas, text="HELLO THERE"))
        lower = _render(tmp_path, canvas, text="HELLO THERE", text_case="lower")
        assert _h(lower) == _h(_render(tmp_path, canvas, text="hello there"))
        title = _render(tmp_path, canvas, text="hELLO tHERE", text_case="title")
        assert _h(title) == _h(_render(tmp_path, canvas, text="Hello There"))
        assert _h(upper) != _h(lower)
        none = _render(tmp_path, canvas, text="Hello", text_case="none")
        assert _h(none) == _h(_render(tmp_path, canvas, text="Hello"))

    def test_letter_spacing_widens_and_negative_narrows(self, tmp_path, canvas):
        def width(**kw):
            box = _opaque_bbox(_render(tmp_path, canvas, text="SPACING", **kw))
            return box[2] - box[0]

        base = width()
        assert width(letter_spacing=0.3) > base + 100
        assert width(letter_spacing=-0.05) < base

    def test_line_spacing_changes_block_height(self, tmp_path, canvas):
        def height(**kw):
            img = _render(
                tmp_path,
                canvas,
                text="one two three four five six",
                max_width_frac=0.3,
                **kw,
            )
            box = _opaque_bbox(img)
            return box[3] - box[1]

        base = height()
        assert height(line_spacing=2.5) > base * 1.4
        assert height(line_spacing=0.6) < base

    def test_default_spacing_values_match_unset(self, tmp_path, canvas):
        # line_spacing=1.15 is the renderer default => identical pixels
        assert _h(
            _render(tmp_path, canvas, text="a b c d e f g h", max_width_frac=0.3, line_spacing=1.15)
        ) == _h(_render(tmp_path, canvas, text="a b c d e f g h", max_width_frac=0.3))


# --- cache digest ---------------------------------------------------------------------


def test_digest_pinned_to_pre_parity_value_for_elements_without_new_fields(monkeypatch):
    from app.config import settings

    edits = SlideEdits(texts=[SlideTextElement(id="a", text="x", size_px=120, stroke_width=3)])
    # Values computed on origin/main before the parity fields existed.
    monkeypatch.setattr(settings, "slide_post_rich_text_enabled", False)
    assert edits_cache_digest(edits) == "8a8d336456522d64"
    monkeypatch.setattr(settings, "slide_post_rich_text_enabled", True)
    assert edits_cache_digest(edits) == "69c4c10202bf87ec"


def test_digest_unchanged_without_new_fields_and_moves_with_each():
    base = SlideEdits(texts=[SlideTextElement(id="a", text="x")])
    # The exact pre-parity payload (no parity keys) still hashes to the same value.
    payload = base.model_dump_json()
    assert "rotation_deg" not in payload and "letter_spacing" not in payload
    seen = {edits_cache_digest(base)}
    for kw in (
        {"rotation_deg": 5},
        {"stroke_color": "#112233"},
        {"shadow_color": "#112233"},
        {"shadow_opacity": 0.3},
        {"background_color": "#FFF0A6"},
        {"editor_preset": "Bold"},
        {"text_case": "upper"},
        {"letter_spacing": 0.1},
        {"line_spacing": 1.5},
    ):
        d = edits_cache_digest(SlideEdits(texts=[SlideTextElement(id="a", text="x", **kw)]))
        assert d not in seen, kw
        seen.add(d)
