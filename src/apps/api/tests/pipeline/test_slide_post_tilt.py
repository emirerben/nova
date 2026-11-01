"""Tilting a slide's photo or video (KRI-1138): the wire, the geometry the iPhone shares, and
the cloud render, through real FFmpeg.

Failure modes this file guards (written before the code):
 L1  an unchanged slide's cache digest / serialized form moves -> every export re-renders
 L2  the turn goes the wrong way (counter-clockwise) or about the wrong point
 L3  cloud and phone disagree on the media size, the corners or the backdrop
 L4  a backdrop is missing, bleeds over the media, or blur renders black
 L5  text, ink, stickers or the watermark turn with the photo (only the media may)
 L6  a video slide ignores the tilt or loses its backdrop on later frames
 L7  tilt breaks crop / Fit / the other canvas shapes (4:5, 1:1, landscape 1920x1080)
 L8  an invalid tilt is persisted (out of range, colour on a non-colour backdrop, unknown key)
 L9  Swift and Python drift apart (the shared fixture)
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

from app.pipeline.slide_post import tilt as tilt_math
from app.pipeline.slide_post.build import (
    FIT_BLUR_SIGMA_PX,
    SLIDE_TILT_COMPOSITOR_VERSION,
    edits_cache_digest,
    normalize_image_slide,
    normalize_video_slide,
    probe_dimensions,
    tilt_key_suffix,
)
from app.schemas.slide_post import SlideEdits, SlideFraming, SlideTilt

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[5] / "tests" / "fixtures" / "slide-tilt-parity" / "v1.json"
    ).read_text()
)
RED, GREEN, BLUE, YELLOW = (230, 20, 20), (20, 200, 20), (20, 20, 230), (240, 230, 20)
QUADRANTS = ((RED, GREEN), (BLUE, YELLOW))


# ── L8 / L9: the wire contract, shared with Swift ────────────────────────────


class TestFixtureContract:
    @pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
    def test_server_geometry_matches_the_fixture(self, case):
        canvas = (case["canvas"]["w"], case["canvas"]["h"])
        media = tilt_math.media_size(
            (case["image"]["w"], case["image"]["h"]), case["crop"], canvas, fit=case["fit"]
        )
        expected = case["expected"]
        assert media[0] == pytest.approx(expected["media"]["w"], abs=1e-6)
        assert media[1] == pytest.approx(expected["media"]["h"], abs=1e-6)
        bounds = tilt_math.rotated_bounds(*media, case["rotation_deg"])
        assert bounds[0] == pytest.approx(expected["rotated_bounds"]["w"], abs=1e-6)
        assert bounds[1] == pytest.approx(expected["rotated_bounds"]["h"], abs=1e-6)
        corners = tilt_math.rotated_corners(*media, case["rotation_deg"], canvas)
        for got, want in zip(corners, expected["corners"], strict=True):
            assert got == pytest.approx(tuple(want), abs=1e-6)
        assert (
            tilt_math.covers_canvas(*media, case["rotation_deg"], canvas)
            is expected["covers_canvas"]
        )

    def test_a_clockwise_turn_moves_the_top_left_corner_toward_the_top_right(self):
        # L2: +90 clockwise sends the top-left corner to where the top-right was.
        case = next(c for c in FIXTURE["cases"] if c["name"] == "fill_quarter_turn_90")
        tl = case["expected"]["corners"][0]
        cx, cy = case["canvas"]["w"] / 2, case["canvas"]["h"] / 2
        mw, mh = case["expected"]["media"]["w"], case["expected"]["media"]["h"]
        assert tl[0] == pytest.approx(cx + mh / 2) and tl[1] == pytest.approx(cy - mw / 2)

    @pytest.mark.parametrize("case", FIXTURE["validation"], ids=lambda c: c["name"])
    def test_validation_verdicts_match_the_fixture(self, case):
        if case["valid"]:
            edits = SlideEdits.model_validate({"tilt": case["tilt"]})
            again = SlideEdits.model_validate(edits.model_dump(mode="json"))
            assert again.model_dump(mode="json") == edits.model_dump(mode="json")
            return
        with pytest.raises(ValidationError) as exc:
            SlideEdits.model_validate({"tilt": case["tilt"]})
        reason = case["reason"]
        if reason == "bounds":
            kinds = {e["type"] for e in exc.value.errors()}
            assert kinds & {
                "greater_than_equal",
                "less_than_equal",
                "string_pattern_mismatch",
                "literal_error",
            }, kinds
        else:
            assert reason in str(exc.value), str(exc.value)

    @pytest.mark.parametrize("case", FIXTURE["normalization"], ids=lambda c: c["name"])
    def test_normalization_matches_the_fixture(self, case):
        edits = SlideEdits.model_validate({"tilt": case["tilt"]})
        assert edits.tilt is None and case["stored"] is None
        assert "tilt" not in edits.model_dump(mode="json")

    def test_caps_match_the_fixture(self):
        assert FIXTURE["caps"]["blur_sigma_px"] == FIT_BLUR_SIGMA_PX
        assert SlideTilt(rotation_deg=FIXTURE["caps"]["rotation_deg_min"]).rotation_deg == -180
        assert SlideTilt(rotation_deg=FIXTURE["caps"]["rotation_deg_max"]).rotation_deg == 180

    def test_the_wire_shape_round_trips_without_a_colour_on_blank_or_blur(self):
        edits = SlideEdits(tilt={"rotation_deg": 12.5, "backdrop": {"kind": "blur"}})
        assert edits.model_dump(mode="json")["tilt"] == {
            "rotation_deg": 12.5,
            "backdrop": {"kind": "blur"},
        }
        # An omitted backdrop is the blank default.
        assert SlideEdits(tilt={"rotation_deg": 5}).tilt.backdrop.kind == "blank"


# ── L1: defaults are byte-identical ──────────────────────────────────────────


class TestDefaultsAreUnchanged:
    def test_cache_digests_are_pinned(self):
        # The same pins test_slide_post_framing.py holds: a tilt field that leaked into the
        # serialized form of an untilted slide would move these and re-render every export.
        assert edits_cache_digest(SlideEdits()) == "a268757d51d16368"
        assert edits_cache_digest(SlideEdits(look_preset="golden_hour")) == "86380fdd5a4ec3aa"
        assert edits_cache_digest(SlideEdits(tilt=None)) == "a268757d51d16368"

    def test_an_upright_tilt_is_no_tilt(self):
        upright = SlideEdits(tilt={"rotation_deg": 0.0, "backdrop": {"kind": "blur"}})
        assert upright.tilt is None
        assert edits_cache_digest(upright) == edits_cache_digest(SlideEdits())
        assert tilt_key_suffix(upright) == ""
        assert tilt_key_suffix(None) == ""
        assert "tilt" not in SlideEdits().model_dump()
        assert "tilt" not in json.loads(SlideEdits().model_dump_json())

    def test_a_tilt_moves_the_digest_and_the_key_suffix(self):
        base = SlideEdits(tilt={"rotation_deg": 10})
        assert tilt_key_suffix(base) == f"_tl{SLIDE_TILT_COMPOSITOR_VERSION}"
        assert edits_cache_digest(base) != edits_cache_digest(SlideEdits())
        assert edits_cache_digest(base) != edits_cache_digest(SlideEdits(tilt={"rotation_deg": 11}))
        assert edits_cache_digest(base) != edits_cache_digest(
            SlideEdits(tilt={"rotation_deg": 10, "backdrop": {"kind": "blur"}})
        )


# ── L2 / L3 / L4 / L7: the real cloud render, against the shared geometry ────


def _quadrant_image(path: Path, size: tuple[int, int] = (800, 600)) -> None:
    img = Image.new("RGB", size)
    w, h = size
    for row in range(2):
        for col in range(2):
            img.paste(
                QUADRANTS[row][col],
                (col * w // 2, row * h // 2, (col + 1) * w // 2, (row + 1) * h // 2),
            )
    img.save(path)


def _pixels(path) -> np.ndarray:
    with Image.open(path) as img:
        return np.asarray(img.convert("RGB")).astype(int)


def _near(pixel, expected, tol: int = 60) -> bool:
    return all(abs(int(a) - int(b)) <= tol for a, b in zip(pixel[:3], expected, strict=True))


def _expected_colour(point, *, canvas, image, crop, fit, rotation_deg):
    """What a canvas pixel should show for a quadrant photo, from the shared geometry alone.

    Returns a quadrant colour, ``"backdrop"``, or None when the point is too close to an edge
    or a quadrant seam to be judged against anti-aliasing.
    """
    mw, mh = tilt_math.media_size(image, crop, canvas, fit=fit)
    rad = math.radians(rotation_deg)
    dx, dy = point[0] - canvas[0] / 2, point[1] - canvas[1] / 2
    mx, my = dx * math.cos(rad) + dy * math.sin(rad), -dx * math.sin(rad) + dy * math.cos(rad)
    margin = 14
    if abs(abs(mx) - mw / 2) < margin or abs(abs(my) - mh / 2) < margin:
        return None
    if abs(mx) > mw / 2 or abs(my) > mh / 2:
        return "backdrop"
    u, v = (mx + mw / 2) / mw, (my + mh / 2) / mh
    cx, cy, cw, chh = (
        (0.0, 0.0, 1.0, 1.0)
        if crop is None
        else (crop["x"], crop["y"], crop["width"], crop["height"])
    )
    su, sv = cx + u * cw, cy + v * chh
    # A quadrant seam: too close to it to judge against anti-aliasing.
    if abs(su - 0.5) * mw / cw < margin or abs(sv - 0.5) * mh / chh < margin:
        return None
    return QUADRANTS[int(sv >= 0.5)][int(su >= 0.5)]


def _grid(canvas, columns=9, rows=15):
    return [
        ((i + 0.5) * canvas[0] / columns, (j + 0.5) * canvas[1] / rows)
        for i in range(columns)
        for j in range(rows)
    ]


def _check_against_geometry(path, *, canvas, image, crop, fit, rotation_deg, backdrop_rgb):
    px = _pixels(path)
    judged = wrong = backdrop_seen = 0
    for point in _grid(canvas):
        want = _expected_colour(
            point, canvas=canvas, image=image, crop=crop, fit=fit, rotation_deg=rotation_deg
        )
        if want is None:
            continue
        # The Kria watermark sits near the bottom-left: not part of the photo.
        if point[0] < canvas[0] * 0.35 and point[1] > canvas[1] * 0.7:
            continue
        got = px[int(point[1]), int(point[0])]
        judged += 1
        if want == "backdrop":
            backdrop_seen += 1
            ok = _near(got, backdrop_rgb)
        else:
            ok = _near(got, want)
        wrong += 0 if ok else 1
    return judged, wrong, backdrop_seen


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestImageRender:
    @pytest.mark.parametrize(
        ("name", "canvas", "crop", "fit", "rotation_deg"),
        [
            ("clockwise_30", (1080, 1920), None, None, 30),
            ("counter_clockwise_25", (1080, 1920), None, None, -25),
            ("quarter_turn", (1080, 1920), None, None, 90),
            ("half_turn", (1080, 1920), None, None, 180),
            ("fit_20", (1080, 1920), None, "black", 20),
            (
                "cropped_fill",
                (1080, 1920),
                {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                None,
                15,
            ),
            ("four_by_five", (1080, 1350), None, None, -40),
            ("square", (1080, 1080), None, "white", 35),
            ("landscape_slide", (1920, 1080), None, None, 20),
        ],
    )
    def test_blank_backdrop_matches_the_shared_geometry(
        self, tmp_path, name, canvas, crop, fit, rotation_deg
    ):
        src = tmp_path / "quad.png"
        _quadrant_image(src)
        out = tmp_path / "out.jpg"
        edits = SlideEdits(
            tilt={"rotation_deg": rotation_deg},
            fit=fit,
            framing=SlideFraming(orientation="landscape") if canvas == (1920, 1080) else None,
        )
        normalize_image_slide(str(src), str(out), canvas=canvas, edits=edits)
        assert probe_dimensions(str(out)) == canvas
        judged, wrong, _ = _check_against_geometry(
            out,
            canvas=canvas,
            image=(800, 600),
            crop=crop,
            fit=fit is not None,
            rotation_deg=rotation_deg,
            backdrop_rgb=(0, 0, 0),
        )
        assert judged >= 40, judged
        assert wrong <= max(1, judged // 40), (wrong, judged)

    def test_a_cropped_region_turns_not_the_whole_photo(self, tmp_path):
        src = tmp_path / "quad.png"
        _quadrant_image(src)
        out = tmp_path / "out.jpg"
        crop = {"x": 0.5, "y": 0.0, "width": 0.5, "height": 0.5}  # only the green quadrant
        edits = SlideEdits(
            tilt={"rotation_deg": 10},
            framing=SlideFraming(source_crop=crop),
        )
        normalize_image_slide(str(src), str(out), canvas=(1080, 1920), edits=edits)
        px = _pixels(out)
        assert _near(px[960, 540], GREEN)
        assert not any(
            _near(px[y, x], c, 40)
            for y in (700, 1200)
            for x in (300, 800)
            for c in (RED, BLUE, YELLOW)
        )

    def test_clockwise_means_the_right_side_goes_down(self, tmp_path):
        # L2: a photo that is white on the left half and black on the right half; a clockwise
        # turn of 90 puts the old LEFT edge on TOP.
        src = tmp_path / "halves.png"
        img = Image.new("RGB", (400, 400), (0, 0, 0))
        img.paste((255, 255, 255), (0, 0, 200, 400))
        img.save(src)
        out = tmp_path / "out.jpg"
        normalize_image_slide(
            str(src), str(out), canvas=(1080, 1920), edits=SlideEdits(tilt={"rotation_deg": 90})
        )
        px = _pixels(out)
        assert px[300, 540].min() > 200 and px[1500, 540].max() < 60

    def test_colour_backdrop_fills_the_uncovered_corners_exactly(self, tmp_path):
        src = tmp_path / "quad.png"
        _quadrant_image(src)
        out = tmp_path / "out.jpg"
        edits = SlideEdits(
            tilt={"rotation_deg": 20, "backdrop": {"kind": "color", "color": "#336699"}},
            fit="black",
        )
        normalize_image_slide(str(src), str(out), canvas=(1080, 1920), edits=edits)
        judged, wrong, backdrop_seen = _check_against_geometry(
            out,
            canvas=(1080, 1920),
            image=(800, 600),
            crop=None,
            fit=True,
            rotation_deg=20,
            backdrop_rgb=(0x33, 0x66, 0x99),
        )
        assert backdrop_seen >= 20 and wrong <= max(1, judged // 40), (wrong, judged)
        px = _pixels(out)
        assert _near(px[40, 40], (0x33, 0x66, 0x99), 6)
        assert _near(px[1880, 1040], (0x33, 0x66, 0x99), 6)

    def test_a_tilt_replaces_the_fit_bars_with_its_own_backdrop(self, tmp_path):
        src = tmp_path / "quad.png"
        _quadrant_image(src)
        white_bars = tmp_path / "white.jpg"
        normalize_image_slide(
            str(src), str(white_bars), canvas=(1080, 1920), edits=SlideEdits(fit="white")
        )
        assert _pixels(white_bars)[40, 540].min() > 240
        tilted = tmp_path / "tilted.jpg"
        normalize_image_slide(
            str(src),
            str(tilted),
            canvas=(1080, 1920),
            edits=SlideEdits(fit="white", tilt={"rotation_deg": 5}),
        )
        assert _pixels(tilted)[40, 540].max() < 40  # blank, not the white Fit bars

    def test_blur_backdrop_is_a_soft_copy_of_the_photo_not_black(self, tmp_path):
        src = tmp_path / "quad.png"
        _quadrant_image(src)
        out = tmp_path / "out.jpg"
        edits = SlideEdits(tilt={"rotation_deg": 20, "backdrop": {"kind": "blur"}}, fit="black")
        normalize_image_slide(str(src), str(out), canvas=(1080, 1920), edits=edits)
        px = _pixels(out)
        # Top-left of the canvas is outside the turned, whole photo: blurred red, not black.
        assert px[40, 40][0] > 120 and px[40, 40][1] < 120
        # The backdrop is blurred: a hard red/green seam in the cover copy is soft.
        row = px[300, :, :]
        steps = np.abs(np.diff(row, axis=0)).max(axis=1)
        assert steps.max() < 120
        # ...and it never bleeds over the media: the photo centre is still a pure quadrant colour.
        assert any(
            _near(px[960 + dy, 540 + dx], c, 40)
            for dy, dx in ((-60, -60), (-60, 60), (60, -60), (60, 60))
            for c in (RED, GREEN, BLUE, YELLOW)
        )

    def test_text_ink_and_stickers_stay_upright_on_a_tilted_photo(self, tmp_path):
        # L5: an axis-aligned red sticker above a photo turned 30 degrees keeps its corners.
        src = tmp_path / "quad.png"
        _quadrant_image(src)
        sticker = tmp_path / "s.png"
        Image.new("RGBA", (100, 100), (255, 0, 0, 255)).save(sticker)
        out = tmp_path / "out.jpg"
        edits = SlideEdits(
            tilt={"rotation_deg": 30},
            stickers=[
                {"id": "s1", "asset_id": "a", "x_frac": 0.5, "y_frac": 0.5, "width_frac": 0.4}
            ],
        )
        normalize_image_slide(
            str(src),
            str(out),
            canvas=(1080, 1920),
            edits=edits,
            sticker_sources={"s1": str(sticker)},
        )
        px = _pixels(out)
        half = int(0.4 * 1080 / 2)
        for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            x, y = 540 + dx * (half - 8), 960 + dy * (half - 8)
            assert px[y, x][0] > 200 and px[y, x][1] < 60, (dx, dy)

    def test_a_look_grades_the_whole_turned_frame_backdrop_included(self, tmp_path):
        src = tmp_path / "quad.png"
        _quadrant_image(src)
        plain = tmp_path / "plain.jpg"
        graded = tmp_path / "graded.jpg"
        tilt = {"rotation_deg": 20, "backdrop": {"kind": "color", "color": "#336699"}}
        normalize_image_slide(
            str(src), str(plain), canvas=(1080, 1920), edits=SlideEdits(tilt=tilt, fit="black")
        )
        normalize_image_slide(
            str(src),
            str(graded),
            canvas=(1080, 1920),
            edits=SlideEdits(tilt=tilt, fit="black", look_preset="golden_hour"),
        )
        assert not np.array_equal(_pixels(plain)[40, 40], _pixels(graded)[40, 40])


# ── L6: video slides ─────────────────────────────────────────────────────────


def _make_video(path: Path, *, duration_s: float = 1.0) -> None:
    # A landscape clip whose left half is red and right half blue, all the way through.
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=0xE61414:s=160x240:r=30:d={duration_s}",
            "-f", "lavfi", "-i", f"color=c=0x1414E6:s=160x240:r=30:d={duration_s}",
            "-filter_complex", "[0:v][1:v]hstack=inputs=2,format=yuv420p[v]",
            "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", str(path),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip


def _frame(video: Path, at: str, png: Path) -> np.ndarray:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            at,
            "-i",
            str(video),
            "-frames:v",
            "1",
            str(png),
        ],
        check=True,
        capture_output=True,
    )
    return _pixels(png)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestVideoRender:
    @pytest.mark.parametrize(
        ("backdrop", "corner"),
        [
            ({"kind": "blank"}, (0, 0, 0)),
            ({"kind": "color", "color": "#33CC66"}, (0x33, 0xCC, 0x66)),
        ],
    )
    def test_the_turned_video_keeps_its_backdrop_on_every_frame(self, tmp_path, backdrop, corner):
        src = tmp_path / "src.mp4"
        out = tmp_path / "out.mp4"
        _make_video(src)
        normalize_video_slide(
            str(src),
            str(out),
            canvas=(1080, 1920),
            edits=SlideEdits(tilt={"rotation_deg": 25, "backdrop": backdrop}),
        )
        assert probe_dimensions(str(out)) == (1080, 1920)
        for at in ("0.05", "0.5", "0.9"):
            px = _frame(out, at, tmp_path / f"f{at}.png")
            assert _near(px[1916, 3], corner, 24), at  # the bottom-left corner is uncovered
            assert _near(px[3, 1076], corner, 24), at  # ...and so is the top-right
            assert not _near(px[960, 540], corner, 40), at  # ...while the media shows at the centre

    def test_the_video_turns_clockwise(self, tmp_path):
        # Red left half / blue right half, turned 90 clockwise: the left (red) half ends on TOP.
        src = tmp_path / "src.mp4"
        out = tmp_path / "out.mp4"
        _make_video(src)
        normalize_video_slide(
            str(src),
            str(out),
            canvas=(1080, 1920),
            edits=SlideEdits(tilt={"rotation_deg": 90}),
        )
        px = _frame(out, "0.5", tmp_path / "f.png")
        assert _near(px[300, 540], RED, 50) and _near(px[1500, 540], BLUE, 50)

    def test_blur_backdrop_on_a_video_is_not_black(self, tmp_path):
        src = tmp_path / "src.mp4"
        out = tmp_path / "out.mp4"
        _make_video(src)
        normalize_video_slide(
            str(src),
            str(out),
            canvas=(1080, 1350),
            edits=SlideEdits(tilt={"rotation_deg": 30, "backdrop": {"kind": "blur"}}),
        )
        assert probe_dimensions(str(out)) == (1080, 1350)
        px = _frame(out, "0.5", tmp_path / "f.png")
        assert px[10, 10].max() > 80
