"""Pasted image stickers on slides (KRI-647 / KRI-460): wire contract, geometry and the
real FFmpeg composite.

Failure modes this file guards (listed before the code was written):
 S1  the frozen wire fixture drifts (a valid sticker stops validating, an invalid one is accepted)
 S2  server geometry disagrees with the fixture iOS asserts too (centre, width, aspect, rotation)
 S3  a slide without stickers changes its serialized form / cache digest -> mass re-render
 S4  the sticker flattens its alpha, distorts its aspect, rotates the wrong way, or the corners
     of a rotated sticker are opaque instead of transparent
 S5  the sticker is composited above the watermark, or the z-order is not list order
 S6  an unreadable / missing sticker aborts the slide instead of being dropped
 S7  changing a sticker's image or geometry reuses the old cached derivative
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

from app.pipeline.slide_post.build import (
    edits_cache_digest,
    normalize_image_slide,
    normalize_video_slide,
    sticker_key_suffix,
)
from app.pipeline.slide_post.stickers import sticker_geometry
from app.schemas.slide_post import SlideEdits, SlideSticker, sticker_asset_ids
from tests.pipeline.test_slide_post_build import _brightest, _make_image, _make_video, _mark_box

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[5]
        / "tests"
        / "fixtures"
        / "slide-sticker-parity"
        / "v1.json"
    ).read_text()
)
_HAS_FFMPEG = shutil.which("ffmpeg") is not None
ASSET = "11111111-1111-1111-1111-111111111111"


def _sticker(**kw) -> dict:
    base = {
        "id": "s1",
        "asset_id": ASSET,
        "x_frac": 0.5,
        "y_frac": 0.5,
        "width_frac": 0.4,
        "rotation_deg": 0,
    }
    return {**base, **kw}


class TestFixtureContract:
    @pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
    def test_server_geometry_matches_the_fixture(self, case):
        sticker = SlideSticker.model_validate({**_sticker(), **case["sticker"]})
        geometry = sticker_geometry(
            (case["canvas"]["w"], case["canvas"]["h"]),
            (case["image"]["w"], case["image"]["h"]),
            sticker,
        )
        expected = case["expected"]
        for name in ("center_x", "center_y", "width_px", "height_px", "rotation_deg"):
            assert getattr(geometry, name) == pytest.approx(expected[name], abs=1e-6), name

    @pytest.mark.parametrize("case", FIXTURE["validation"], ids=lambda c: c["name"])
    def test_validation_verdicts_match_the_fixture(self, case):
        if case["valid"]:
            edits = SlideEdits.model_validate({"stickers": case["stickers"]})
            # Round trip: dump -> validate -> dump is the identity (the stored draft shape);
            # an empty list is valid and serializes to no key at all.
            again = SlideEdits.model_validate(edits.model_dump(mode="json"))
            assert again.model_dump(mode="json") == edits.model_dump(mode="json")
            assert ("stickers" in edits.model_dump(mode="json")) == bool(case["stickers"])
            return
        with pytest.raises(ValidationError) as exc:
            SlideEdits.model_validate({"stickers": case["stickers"]})
        message = str(exc.value)
        reason = case["reason"]
        if reason == "bounds":
            kinds = {e["type"] for e in exc.value.errors()}
            assert kinds & {
                "greater_than_equal",
                "less_than_equal",
                "string_too_short",
                "string_too_long",
            }, kinds
        else:
            assert reason in message, message

    def test_caps_match_the_fixture(self):
        from app.schemas.slide_post import MAX_SLIDE_STICKERS

        assert FIXTURE["caps"]["max_stickers_per_slide"] == MAX_SLIDE_STICKERS

    def test_unknown_sticker_keys_are_refused(self):
        with pytest.raises(ValidationError):
            SlideEdits.model_validate({"stickers": [_sticker(skew=1)]})


class TestDigestAndSerialization:
    def test_no_stickers_key_is_serialized_and_digests_do_not_move(self):
        plain = SlideEdits(look_preset="none")
        for empty in (SlideEdits(stickers=None), SlideEdits(stickers=[])):
            assert "stickers" not in empty.model_dump(mode="json")
            assert edits_cache_digest(empty) == edits_cache_digest(plain)
            assert sticker_key_suffix(empty) == ""
        assert sticker_key_suffix(None) == ""

    def test_geometry_and_image_identity_move_the_digest(self):
        base = SlideEdits(stickers=[_sticker()])
        moved = SlideEdits(stickers=[_sticker(x_frac=0.6)])
        fingerprints = {"s1": "fp-a"}
        assert sticker_key_suffix(base) == "_st1"
        assert edits_cache_digest(base, fingerprints) != edits_cache_digest(None)
        assert edits_cache_digest(base, fingerprints) != edits_cache_digest(moved, fingerprints)
        assert edits_cache_digest(base, fingerprints) != edits_cache_digest(base, {"s1": "fp-b"})
        # A dropped (unresolvable) sticker hashes differently from a resolved one.
        assert edits_cache_digest(base, fingerprints) != edits_cache_digest(base, {})

    def test_sticker_asset_ids_lists_every_referenced_asset(self):
        from app.schemas.slide_post import SlidePostDraft, SlideRef

        draft = SlidePostDraft(
            platform_profile="tiktok_photo",
            slides=[
                SlideRef(
                    id="a",
                    asset_id="00000000-0000-0000-0000-00000000000a",
                    kind="image",
                    edits=SlideEdits(stickers=[_sticker()]),
                ),
                SlideRef(id="b", asset_id="00000000-0000-0000-0000-00000000000b", kind="image"),
            ],
        )
        assert {str(i) for i in sticker_asset_ids(draft)} == {ASSET}
        assert sticker_asset_ids(None) == set()


# ---- Real FFmpeg composite --------------------------------------------------------------

CANVAS = (1080, 1920)
PHOTO = (60, 60, 60)


def _pixels(path) -> np.ndarray:
    with Image.open(path) as img:
        return np.asarray(img.convert("RGB")).astype(int)


def _png(path: Path, size: tuple[int, int], paint) -> Path:
    """An RGBA image; ``paint(arr)`` fills an (h, w, 4) uint8 array."""
    w, h = size
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    paint(arr)
    Image.fromarray(arr, "RGBA").save(path)
    return path


def _render(tmp_path, edits, sources, *, name="out", size=CANVAS):
    src = tmp_path / "photo.png"
    if not src.exists():
        _make_image(src, size[0], size[1], color=PHOTO)
    out = tmp_path / f"{name}.jpg"
    normalize_image_slide(str(src), str(out), canvas=size, edits=edits, sticker_sources=sources)
    return out


def _changed_bbox(a: np.ndarray, b: np.ndarray, threshold: int = 40):
    ys, xs = np.where(np.abs(a - b).max(axis=2) > threshold)
    return xs.min(), ys.min(), xs.max(), ys.max()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestRealFfmpegComposite:
    def test_sticker_lands_at_its_centre_with_alpha_and_aspect_preserved(self, tmp_path):
        # 400x200: left half red, right half green, top-left 60x60 corner fully transparent,
        # and a 40x40 half-transparent white square at the bottom-right.
        def paint(arr):
            arr[:, :200] = (255, 0, 0, 255)
            arr[:, 200:] = (0, 255, 0, 255)
            arr[:60, :60] = (0, 0, 0, 0)
            arr[160:, 360:] = (255, 255, 255, 128)

        sticker = _png(tmp_path / "s.png", (400, 200), paint)
        edits = SlideEdits(stickers=[_sticker(x_frac=0.5, y_frac=0.4, width_frac=0.4)])
        out = _render(tmp_path, edits, {"s1": str(sticker)})
        plain = _render(tmp_path, None, None, name="plain")
        px = _pixels(out)
        # 432x216 displayed, centre (540, 768): left of centre red, right green.
        assert px[768, 540 - 100][0] > 220 and px[768, 540 - 100][1] < 40
        assert px[768, 540 + 100][1] > 220 and px[768, 540 + 100][0] < 40
        # Aspect preserved: the opaque bbox is 432x216 (the transparent corner is not part of
        # the bbox's extremes because the rest of its row/column is opaque).
        x0, y0, x1, y1 = _changed_bbox(_pixels(plain), px)
        assert abs((x1 - x0 + 1) - 432) <= 3 and abs((y1 - y0 + 1) - 216) <= 3
        assert abs((x0 + x1) / 2 - 540) <= 2 and abs((y0 + y1) / 2 - 768) <= 2
        # Transparent corner shows the photo, not black or the sticker colour.
        left, top = 540 - 216, 768 - 108
        corner = px[top + 12, left + 12]
        assert np.abs(corner - np.array(PHOTO)).max() <= 6
        # Alpha preserved (not flattened): 50% white over the grey photo lands in between.
        blended = px[top + 216 - 10, left + 432 - 10]
        assert 130 <= blended[0] <= 170 and abs(blended[0] - blended[1]) <= 4

    def test_rotation_is_clockwise_with_transparent_corners_and_a_rotated_bbox(self, tmp_path):
        def paint(arr):
            arr[:, :] = (255, 0, 0, 255)
            arr[:, 300:] = (0, 255, 0, 255)  # the right end is green

        sticker = _png(tmp_path / "s.png", (400, 200), paint)
        plain = _render(tmp_path, None, None, name="plain")

        quarter = _render(
            tmp_path,
            SlideEdits(stickers=[_sticker(rotation_deg=90)]),
            {"s1": str(sticker)},
            name="q",
        )
        px = _pixels(quarter)
        # Clockwise 90: the right (green) end moves BELOW the centre; the left end goes up.
        assert px[960 + 150, 540][1] > 200 and px[960 + 150, 540][0] < 60
        assert px[960 - 150, 540][0] > 200 and px[960 - 150, 540][1] < 60
        x0, y0, x1, y1 = _changed_bbox(_pixels(plain), px)
        assert abs((x1 - x0 + 1) - 216) <= 4 and abs((y1 - y0 + 1) - 432) <= 4

        tilted = _render(
            tmp_path,
            SlideEdits(stickers=[_sticker(rotation_deg=30)]),
            {"s1": str(sticker)},
            name="t",
        )
        tx = _pixels(tilted)
        x0, y0, x1, y1 = _changed_bbox(_pixels(plain), tx)
        w, h = 432, 216
        rad = math.radians(30)
        want_w = w * math.cos(rad) + h * math.sin(rad)
        want_h = w * math.sin(rad) + h * math.cos(rad)
        assert abs((x1 - x0 + 1) - want_w) <= 5 and abs((y1 - y0 + 1) - want_h) <= 5
        # The rotated bbox's own corners are transparent: they show the photo.
        assert np.abs(tx[y0 + 3, x0 + 3] - np.array(PHOTO)).max() <= 6
        assert np.abs(tx[y1 - 3, x1 - 3] - np.array(PHOTO)).max() <= 6

    def test_sticker_is_clipped_by_the_canvas_edge_not_moved(self, tmp_path):
        sticker = _png(
            tmp_path / "s.png", (100, 100), lambda a: a.__setitem__(slice(None), (255, 0, 0, 255))
        )
        edits = SlideEdits(stickers=[_sticker(x_frac=0.0, y_frac=0.0, width_frac=0.4)])
        px = _pixels(_render(tmp_path, edits, {"s1": str(sticker)}))
        # Centre on the top-left corner: only the bottom-right quarter (216x216) is visible.
        assert px[100, 100][0] > 220 and px[215, 215][0] > 220
        assert np.abs(px[230, 230] - np.array(PHOTO)).max() <= 6
        assert np.abs(px[100, 230] - np.array(PHOTO)).max() <= 6

    def test_list_order_is_z_order_and_the_watermark_stays_on_top(self, tmp_path):
        red = _png(
            tmp_path / "r.png", (100, 100), lambda a: a.__setitem__(slice(None), (255, 0, 0, 255))
        )
        blue = _png(
            tmp_path / "b.png", (100, 100), lambda a: a.__setitem__(slice(None), (0, 0, 255, 255))
        )
        edits = SlideEdits(
            stickers=[
                _sticker(id="r", x_frac=0.45, y_frac=0.4, width_frac=0.3),
                _sticker(id="b", x_frac=0.55, y_frac=0.4, width_frac=0.3),
            ]
        )
        px = _pixels(_render(tmp_path, edits, {"r": str(red), "b": str(blue)}))
        overlap = px[768, 540]  # inside both
        assert overlap[2] > 220 and overlap[0] < 40  # last = blue on top
        assert px[768, 540 - 120][0] > 220  # only red here

        # A full-canvas black sticker may not blank the Kria mark (the mark is composited last).
        black = _png(
            tmp_path / "k.png", (100, 100), lambda a: a.__setitem__(slice(None), (0, 0, 0, 255))
        )
        cover = SlideEdits(stickers=[_sticker(id="k", width_frac=1.0, y_frac=0.5)])
        out = _render(tmp_path, cover, {"k": str(black)}, name="cover")
        # The black sticker is 1080x1080 centred, so it covers y 420..1500; the mark's box
        # (y ~ 1415..1475) lies inside it.
        left, top, right, bottom = _mark_box(CANVAS)
        assert top > 420 and bottom < 1500
        assert _brightest(out, (left, top, right, bottom)) > 60

    def test_a_missing_or_unreadable_sticker_is_dropped_not_fatal(self, tmp_path):
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"definitely not an image")
        edits = SlideEdits(stickers=[_sticker(id="a"), _sticker(id="b", x_frac=0.2)])
        out = _render(tmp_path, edits, {"a": str(bad)})  # "b" has no source at all
        plain = _render(tmp_path, None, None, name="plain")
        assert np.array_equal(_pixels(out), _pixels(plain))

    def test_heic_style_sources_and_exif_orientation_are_upright(self, tmp_path):
        # A JPEG with EXIF orientation 6 (rotate 90 cw to display): the stored 200x100 raster
        # displays as 100x200, so a width_frac=0.2 sticker is 216 px wide and 432 tall.
        img = Image.new("RGB", (200, 100), (255, 0, 0))
        exif = Image.Exif()
        exif[0x0112] = 6
        path = tmp_path / "o.jpg"
        img.save(path, exif=exif)
        edits = SlideEdits(stickers=[_sticker(width_frac=0.2)])
        out = _render(tmp_path, edits, {"s1": str(path)})
        plain = _render(tmp_path, None, None, name="plain")
        x0, y0, x1, y1 = _changed_bbox(_pixels(plain), _pixels(out))
        assert abs((x1 - x0 + 1) - 216) <= 4 and abs((y1 - y0 + 1) - 432) <= 4

    def test_video_slide_carries_the_sticker_over_every_frame(self, tmp_path):
        src = tmp_path / "src.mp4"
        out = tmp_path / "out.mp4"
        frame = tmp_path / "frame.png"
        _make_video(src, duration_s=1.0, color="black")
        sticker = _png(
            tmp_path / "s.png", (100, 100), lambda a: a.__setitem__(slice(None), (255, 0, 0, 255))
        )
        normalize_video_slide(
            str(src),
            str(out),
            canvas=CANVAS,
            edits=SlideEdits(stickers=[_sticker(width_frac=0.3, rotation_deg=45)]),
            sticker_sources={"s1": str(sticker)},
        )
        for at in ("0.1", "0.8"):
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", at, "-i", str(out)]
                + ["-frames:v", "1", str(frame)],
                check=True,
                capture_output=True,
            )
            px = _pixels(frame)
            assert px[960, 540][0] > 180 and px[960, 540][1] < 60, at
            assert px[100, 100].max() < 40, at


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
def test_stickers_sit_above_both_text_and_ink(tmp_path, monkeypatch):
    """Stack: photo, text, ink, stickers, watermark. A blue sticker over a red text box and a
    green stroke at the same spot must win over both."""
    from app.config import settings
    from app.schemas.slide_post import SlideTextElement

    monkeypatch.setattr(settings, "slide_post_rich_text_enabled", True)
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
        "strokes": [
            {
                "color": "#00FF00",
                "opacity": 1.0,
                "width_frac": 0.08,
                "texture": "pen",
                "seed": 3,
                "points": [[0.05, 0.5], [0.95, 0.5]],
            }
        ],
    }
    blue = _png(
        tmp_path / "b.png", (100, 100), lambda a: a.__setitem__(slice(None), (0, 0, 255, 255))
    )
    edits = SlideEdits(
        texts=[text], drawing=ink, stickers=[_sticker(x_frac=0.5, y_frac=0.5, width_frac=0.2)]
    )
    px = _pixels(_render(tmp_path, edits, {"s1": str(blue)}))
    assert px[960, 540][2] > 220 and px[960, 540][0] < 40 and px[960, 540][1] < 40
    # Outside the sticker the ink is still above the text (existing order is unchanged).
    plain_ink = _pixels(_render(tmp_path, SlideEdits(texts=[text], drawing=ink), None, name="i"))
    assert plain_ink[960, 540][1] > 200 and plain_ink[960, 540][0] < 80
