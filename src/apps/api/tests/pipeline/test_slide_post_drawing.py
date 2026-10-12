"""Static freehand drawings on slides (KRI-589 PR2): wire contract, painter geometry,
and the real FFmpeg composite.

Failure modes this file guards (listed before the code was written):
 D1  the frozen wire fixture drifts: a valid drawing stops validating or round-tripping
     byte-identically, or an invalid one (caps, unknown keys, bad aspect) is accepted
 D2  a drawing-free draft changes its serialized form / cache digest -> mass re-render
 D3  the server painter disagrees with the iOS painter on where ink lands (aspect-fit,
     letterboxing, width) -> a drawing exports somewhere other than where it was drawn
 D4  a render is not deterministic (chalk grain / spray dots keyed on anything but
     (seed, arc length)) -> the content-addressed derivative would churn
 D5  the ink is composited above the watermark / changes pixels outside the stroke
 D6  a drawn slide reuses an undrawn slide's cached derivative
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from app.agents._schemas.drawing import DrawingStroke
from app.config import settings
from app.pipeline.slide_post import build
from app.pipeline.slide_post.build import (
    drawing_key_suffix,
    edits_cache_digest,
    normalize_image_slide,
    normalize_video_slide,
)
from app.pipeline.slide_post.drawing_png import (
    fit_rect,
    render_drawing_png,
    render_drawing_rgba,
)
from app.schemas.slide_post import (
    SlideDrawing,
    SlideEdits,
    SlidePostDraft,
    SlideRef,
)
from tests.pipeline.test_slide_post_build import _brightest, _make_image, _make_video, _mark_box

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[5]
        / "tests"
        / "fixtures"
        / "slide-drawing-parity"
        / "v1.json"
    ).read_text()
)
_HAS_FFMPEG = shutil.which("ffmpeg") is not None
ASSET = "00000000-0000-0000-0000-000000000001"
ALL_VALID = [pytest.param(case["drawing"], id=case["name"]) for case in FIXTURE["drawings"]]
ALL_INVALID = [pytest.param(case["drawing"], id=case["name"]) for case in FIXTURE["invalid"]]
GEOMETRY = [pytest.param(case, id=case["name"]) for case in FIXTURE["geometry"]]


def _stroke(points, *, width_frac=0.02, texture="pen", color="#FF3B30", seed=7, opacity=1.0):
    return {
        "color": color,
        "opacity": opacity,
        "width_frac": width_frac,
        "texture": texture,
        "seed": seed,
        "points": points,
    }


# ── D1: the frozen wire contract ─────────────────────────────────────────────


class TestWireContract:
    def test_caps_match_the_fixture(self):
        from app.agents._schemas import drawing as drawing_schema

        caps = FIXTURE["caps"]
        assert caps["max_strokes_per_slide"] == drawing_schema.MAX_STROKES_PER_DRAWING
        assert caps["max_points_per_slide"] == drawing_schema.MAX_POINTS_PER_DRAWING
        assert caps["max_points_per_post"] == drawing_schema.MAX_POINTS_PER_VARIANT

    @pytest.mark.parametrize("raw", ALL_VALID)
    def test_valid_drawings_validate_and_round_trip_byte_identically(self, raw):
        model = SlideDrawing.model_validate(raw)
        assert json.dumps(model.model_dump(mode="json"), sort_keys=True) == json.dumps(
            raw, sort_keys=True
        )
        # ...also through a whole draft (the shape the PUT and the binding digest see).
        draft = SlidePostDraft(
            platform_profile="tiktok_photo",
            slides=[SlideRef(id="a", asset_id=ASSET, kind="image", edits={"drawing": raw})],
        )
        dumped = draft.model_dump(mode="json")
        assert dumped["slides"][0]["edits"]["drawing"] == raw
        again = SlidePostDraft.model_validate(dumped).model_dump(mode="json")
        assert again == dumped

    @pytest.mark.parametrize("raw", ALL_INVALID)
    def test_invalid_drawings_are_rejected(self, raw):
        with pytest.raises(ValidationError):
            SlideDrawing.model_validate(raw)

    def test_post_wide_point_budget_is_enforced_across_slides(self):
        # Each slide is inside the 3000-point cap; together they exceed 8000.
        points = [[0.5, (i % 100) / 100] for i in range(1000)]
        strokes = [_stroke(points, seed=s) for s in range(3)]  # 3000 points per slide
        drawing = {"canvas_aspect": 0.5625, "strokes": strokes}
        slides = [
            SlideRef(id=f"s{i}", asset_id=ASSET, kind="image", edits={"drawing": drawing})
            for i in range(3)
        ]
        SlidePostDraft(platform_profile="tiktok_photo", slides=slides[:2])  # 6000: fine
        with pytest.raises(ValidationError, match="8000"):
            SlidePostDraft(platform_profile="tiktok_photo", slides=slides)  # 9000

    def test_validate_dump_is_idempotent_for_unrounded_input(self):
        raw = {
            "canvas_aspect": 0.5625,
            "strokes": [_stroke([[0.123456, 0.654321], [0.5, 0.5]], color="#ff3b30")],
        }
        once = SlideDrawing.model_validate(raw).model_dump(mode="json")
        assert once["strokes"][0]["points"][0] == [0.1235, 0.6543]
        assert once["strokes"][0]["color"] == "#FF3B30"
        assert SlideDrawing.model_validate(once).model_dump(mode="json") == once


# ── D2: drawing-free drafts are byte-identical ───────────────────────────────


class TestDrawingFreeDraftsAreUnchanged:
    def test_no_drawing_key_is_serialized_and_digests_do_not_move(self):
        assert "drawing" not in SlideEdits().model_dump()
        assert "drawing" not in json.loads(SlideEdits(drawing=None).model_dump_json())
        draft = SlidePostDraft(
            platform_profile="tiktok_photo",
            slides=[SlideRef(id="a", asset_id=ASSET, kind="image", edits=SlideEdits())],
        )
        assert "drawing" not in draft.model_dump(mode="json")["slides"][0]["edits"]
        # The same pins test_slide_post_sizes.py / test_slide_post_framing.py hold.
        assert edits_cache_digest(SlideEdits()) == "a268757d51d16368"
        assert edits_cache_digest(SlideEdits(look_preset="golden_hour")) == "86380fdd5a4ec3aa"

    def test_a_drawing_moves_the_digest_and_the_normalized_key(self):
        drawn = SlideEdits(drawing=FIXTURE["drawings"][0]["drawing"])
        assert edits_cache_digest(drawn) != edits_cache_digest(SlideEdits())
        other = SlideEdits(drawing=FIXTURE["drawings"][3]["drawing"])
        assert edits_cache_digest(drawn) != edits_cache_digest(other)
        # ...and only a drawn slide grows the painter-version suffix (D6).
        assert drawing_key_suffix(drawn) == f"_dr{build.SLIDE_DRAWING_PAINTER_VERSION}"
        assert drawing_key_suffix(SlideEdits()) == ""
        assert drawing_key_suffix(None) == ""


# ── D3: painter geometry against the contract ────────────────────────────────


def _opaque_bbox(rgba: np.ndarray, *, threshold: int = 128) -> tuple[int, int, int, int]:
    ys, xs = np.where(rgba[..., 3] >= threshold)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


class TestPainterGeometry:
    @pytest.mark.parametrize("case", GEOMETRY)
    def test_fit_rectangle_matches_the_contract(self, case):
        canvas = tuple(case["canvas"])
        ox, oy, fw, fh = fit_rect(canvas, case["canvas_aspect"])
        assert (ox, oy) == pytest.approx(case["expected_fit_origin"], abs=0.01)
        assert (fw, fh) == pytest.approx(case["expected_fit_size"], abs=0.01)

    @pytest.mark.parametrize("case", GEOMETRY)
    def test_ink_lands_inside_the_fit_rectangle_and_reaches_its_corners(self, case):
        canvas = tuple(case["canvas"])
        drawing = SlideDrawing.model_validate(
            {
                "canvas_aspect": case["canvas_aspect"],
                "strokes": [_stroke(case["points"], width_frac=case["width_frac"])],
            }
        )
        rgba = render_drawing_rgba(drawing, canvas=canvas)
        assert rgba.shape == (canvas[1], canvas[0], 4)
        half = case["expected_width_px"] / 2
        ox, oy = case["expected_fit_origin"]
        fw, fh = case["expected_fit_size"]
        # The polyline runs corner to corner of the authoring canvas, and a round cap
        # reaches half a stroke width past each end: that is the whole ink footprint.
        want = (
            max(0, ox - half),
            max(0, oy - half),
            min(canvas[0], ox + fw + half),
            min(canvas[1], oy + fh + half),
        )
        got = _opaque_bbox(rgba)
        assert got == pytest.approx(want, abs=2.0)
        # Nothing at all outside the fit rectangle grown by the stroke's reach.
        ink = rgba[..., 3] > 0
        pad = math.ceil(half) + 2
        x0, y0 = max(0, int(ox) - pad), max(0, int(oy) - pad)
        x1, y1 = min(canvas[0], int(ox + fw) + pad), min(canvas[1], int(oy + fh) + pad)
        outside = ink.copy()
        outside[y0:y1, x0:x1] = False
        assert not outside.any()

    @pytest.mark.parametrize("case", GEOMETRY)
    def test_interior_points_and_width_match_the_contract(self, case):
        # A straight two-point stroke passes exactly through its endpoints, so it pins both
        # the point mapping and the width (area = length * width + a round cap's disc).
        canvas = tuple(case["canvas"])
        a, b = case["points"][1], case["points"][2]
        pa, pb = case["expected_points_px"][1], case["expected_points_px"][2]
        drawing = SlideDrawing.model_validate(
            {
                "canvas_aspect": case["canvas_aspect"],
                "strokes": [_stroke([a, b], width_frac=case["width_frac"])],
            }
        )
        rgba = render_drawing_rgba(drawing, canvas=canvas)
        mid = (round((pa[0] + pb[0]) / 2), round((pa[1] + pb[1]) / 2))
        assert rgba[mid[1], mid[0], 3] == 255
        for end in (pa, pb):
            assert rgba[round(end[1]), round(end[0]), 3] == 255
        assert rgba[0, 0, 3] == 0
        length = math.dist(pa, pb)
        area = float(rgba[..., 3].sum()) / 255
        width = case["expected_width_px"]
        assert area == pytest.approx(length * width + math.pi * width * width / 4, rel=0.04)
        ink_box = _opaque_bbox(rgba)
        half = width / 2
        assert ink_box[0] == pytest.approx(min(pa[0], pb[0]) - half, abs=2.0)
        assert ink_box[2] == pytest.approx(max(pa[0], pb[0]) + half, abs=2.0)
        assert ink_box[1] == pytest.approx(min(pa[1], pb[1]) - half, abs=2.0)
        assert ink_box[3] == pytest.approx(max(pa[1], pb[1]) + half, abs=2.0)

    def test_stroke_colour_and_opacity_reach_the_pixels(self):
        drawing = SlideDrawing.model_validate(
            {
                "canvas_aspect": 0.5625,
                "strokes": [
                    _stroke([[0.1, 0.3], [0.9, 0.3]], color="#32D4FF", width_frac=0.03),
                    _stroke([[0.1, 0.6], [0.9, 0.6]], opacity=0.5, width_frac=0.03),
                ],
            }
        )
        rgba = render_drawing_rgba(drawing, canvas=(1080, 1920))
        assert tuple(rgba[int(0.3 * 1920), 540]) == (0x32, 0xD4, 0xFF, 255)
        r, g, b, a = (int(v) for v in rgba[int(0.6 * 1920), 540])
        # Half-alpha un-premultiplication rounds a level or two.
        assert (r, g, b) == pytest.approx((0xFF, 0x3B, 0x30), abs=2)
        assert a == pytest.approx(128, abs=2)


# ── D4: determinism ──────────────────────────────────────────────────────────


class TestDeterminism:
    def test_every_texture_renders_identically_twice(self):
        drawing = SlideDrawing.model_validate(
            next(c for c in FIXTURE["drawings"] if c["name"] == "every-texture")["drawing"]
        )
        first = render_drawing_rgba(drawing, canvas=(1080, 1920))
        second = render_drawing_rgba(drawing, canvas=(1080, 1920))
        assert np.array_equal(first, second)
        assert first[..., 3].any()

    def test_png_files_are_byte_identical(self, tmp_path):
        drawing = SlideDrawing.model_validate(FIXTURE["drawings"][1]["drawing"])
        a, b = tmp_path / "a.png", tmp_path / "b.png"
        render_drawing_png(drawing, str(a), canvas=(1080, 1350))
        render_drawing_png(drawing, str(b), canvas=(1080, 1350))
        assert a.read_bytes() == b.read_bytes()

    @pytest.mark.parametrize("texture", ["chalk", "spray"])
    def test_grain_follows_the_seed(self, texture):
        def render(seed):
            return render_drawing_rgba(
                SlideDrawing.model_validate(
                    {
                        "canvas_aspect": 0.5625,
                        "strokes": [
                            _stroke(
                                [[0.1, 0.5], [0.9, 0.5]],
                                texture=texture,
                                width_frac=0.04,
                                seed=seed,
                            )
                        ],
                    }
                ),
                canvas=(1080, 1920),
            )

        assert np.array_equal(render(3), render(3))
        assert not np.array_equal(render(3), render(4))

    def test_a_dot_stroke_paints_a_dot(self):
        drawing = SlideDrawing.model_validate(
            {"canvas_aspect": 0.5625, "strokes": [_stroke([[0.5, 0.5], [0.5, 0.5]])]}
        )
        rgba = render_drawing_rgba(drawing, canvas=(1080, 1920))
        assert rgba[960, 540, 3] == 255
        x0, y0, x1, y1 = _opaque_bbox(rgba)
        assert x1 - x0 == pytest.approx(21.6, abs=2) and y1 - y0 == pytest.approx(21.6, abs=2)


# ── D5/D6: the real FFmpeg composite ─────────────────────────────────────────

_INK = {
    "canvas_aspect": 0.5625,
    "strokes": [_stroke([[0.15, 0.3], [0.85, 0.3]], width_frac=0.04, color="#FFFFFF")],
}


def _pixels(path) -> np.ndarray:
    with Image.open(path) as img:
        return np.asarray(img.convert("RGB")).astype(int)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestRealFfmpegComposite:
    def test_drawn_image_slide_differs_only_inside_the_ink(self, tmp_path):
        canvas = (1080, 1920)
        src = tmp_path / "src.png"
        _make_image(src, 1080, 1920, color=(20, 20, 20))
        plain, drawn = tmp_path / "plain.jpg", tmp_path / "drawn.jpg"
        normalize_image_slide(str(src), str(plain), canvas=canvas)
        normalize_image_slide(str(src), str(drawn), canvas=canvas, edits=SlideEdits(drawing=_INK))
        diff = np.abs(_pixels(plain) - _pixels(drawn)).max(axis=2)
        ys, xs = np.where(diff > 4)
        assert len(ys), "the drawing left no mark"
        # The stroke is a horizontal bar centred on y = 0.3 * 1920 from x = 0.15..0.85 (+caps).
        half = 0.04 * 1080 / 2
        cy = 0.3 * 1920
        assert ys.min() >= cy - half - 4 and ys.max() <= cy + half + 4
        assert xs.min() >= 0.15 * 1080 - half - 4 and xs.max() <= 0.85 * 1080 + half + 4
        assert _pixels(drawn)[int(cy), 540].min() > 200  # the white ink
        assert _pixels(plain)[int(cy), 540].max() < 60
        # JPEG blocks leak a few levels sideways; far from the ink nothing changed at all.
        far = diff.copy()
        far[int(cy - half - 40) : int(cy + half + 40), :] = 0
        assert far.max() <= 2

    def test_ink_is_painted_under_the_watermark_and_over_the_photo(self, tmp_path):
        canvas = (1080, 1920)
        src = tmp_path / "src.png"
        _make_image(src, 1080, 1920, color=(0, 0, 0))
        left, top, right, bottom = _mark_box(canvas)
        # A fat BLACK stroke through the mark's box: had it landed above the watermark it
        # would blank the mark (the same trick the text-vs-watermark test uses).
        y = (top + bottom) / 2 / 1920
        ink = {
            "canvas_aspect": 0.5625,
            "strokes": [
                _stroke([[0.0, y], [0.6, y]], width_frac=0.08, color="#000000", seed=1),
            ],
        }
        # Precondition: the ink alone is opaque over the whole mark box.
        layer = render_drawing_rgba(SlideDrawing.model_validate(ink), canvas=canvas)
        assert layer[top:bottom, left:right, 3].min() == 255
        out = tmp_path / "out.jpg"
        normalize_image_slide(str(src), str(out), canvas=canvas, edits=SlideEdits(drawing=ink))
        assert _brightest(out, _mark_box(canvas)) > 60

    def test_ink_sits_above_rich_text(self, tmp_path, monkeypatch):
        from app.schemas.slide_post import SlideTextElement

        monkeypatch.setattr(settings, "slide_post_rich_text_enabled", True)
        canvas = (1080, 1920)
        src = tmp_path / "src.png"
        _make_image(src, 1080, 1920, color=(0, 0, 0))
        text = SlideTextElement(
            id="t",
            text="COVER",
            position="custom",
            x_frac=0.5,
            y_frac=0.5,
            color="#FF0000",
            background_color="#FF0000",
            size_px=200,
        )
        ink = {
            "canvas_aspect": 0.5625,
            "strokes": [_stroke([[0.05, 0.5], [0.95, 0.5]], width_frac=0.08, color="#00FF00")],
        }
        out = tmp_path / "out.jpg"
        normalize_image_slide(
            str(src), str(out), canvas=canvas, edits=SlideEdits(texts=[text], drawing=ink)
        )
        r, g, b = _pixels(out)[960, 540]
        assert g > 200 and r < 80 and b < 80

    def test_square_canvas_letterboxes_a_portrait_drawing(self, tmp_path):
        canvas = (1080, 1080)
        src = tmp_path / "src.png"
        _make_image(src, 1080, 1080, color=(20, 20, 20))
        plain, drawn = tmp_path / "plain.jpg", tmp_path / "drawn.jpg"
        normalize_image_slide(str(src), str(plain), canvas=canvas)
        normalize_image_slide(
            str(src),
            str(drawn),
            canvas=canvas,
            edits=SlideEdits(
                drawing={
                    "canvas_aspect": 0.5625,
                    "strokes": [
                        _stroke([[0.0, 0.5], [1.0, 0.5]], width_frac=0.03, color="#FFFFFF")
                    ],
                }
            ),
        )
        ys, xs = np.where(np.abs(_pixels(plain) - _pixels(drawn)).max(axis=2) > 4)
        ox, _, fw, _ = fit_rect(canvas, 0.5625)  # 236.25 .. 843.75
        assert xs.min() >= ox - 20 and xs.max() <= ox + fw + 20

    def test_drawn_video_slide_carries_the_ink_and_keeps_its_size(self, tmp_path):
        canvas = (1080, 1920)
        src = tmp_path / "src.mp4"
        out = tmp_path / "out.mp4"
        frame = tmp_path / "frame.png"
        _make_video(src, duration_s=1.0, color="black")
        normalize_video_slide(str(src), str(out), canvas=canvas, edits=SlideEdits(drawing=_INK))
        assert build.probe_dimensions(str(out)) == canvas
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", "0.6", "-i", str(out)]
            + ["-frames:v", "1", str(frame)],
            check=True,
            capture_output=True,
        )
        assert _pixels(frame)[int(0.3 * 1920), 540].min() > 150
        assert _pixels(frame)[int(0.7 * 1920), 540].max() < 40


def test_the_stroke_model_stays_the_persisted_wire() -> None:
    """DrawingStroke is persisted by the video drawing lane AND by every stored slide draft, and
    `parse_slide_post` fails closed to "no draft" on an invalid stroke. Evolve it only additively
    with defaults; a new required field or a tighter bound would blank every draft that has ink
    (docs/pipelines/slide-post.md, "Slide drawings")."""
    assert set(DrawingStroke.model_fields) == {
        "color",
        "opacity",
        "width_frac",
        "texture",
        "seed",
        "points",
    }
    # Everything but the colour, width and points has a default, so an older payload still parses.
    required = {name for name, field in DrawingStroke.model_fields.items() if field.is_required()}
    assert required == {"color", "width_frac", "points"}
