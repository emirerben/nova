"""Per-slide framing (KRI-44): crop + landscape canvas, rendered through real FFmpeg.

Each test drives the public render functions and inspects the pixels they
produce, so it fails on a wrong region, a stretched slide or a changed cache key.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from app.kria.framing import cover_placement, crop_for_focus
from app.pipeline.slide_post.build import (
    BundleSlideFile,
    build_post_manifest,
    edits_cache_digest,
    normalize_image_slide,
    probe_dimensions,
    render_preview_segment,
    render_text_element_png,
    slide_canvas,
)
from app.schemas.slide_post import (
    SlideEdits,
    SlideFraming,
    SlidePostDraft,
    SlideRef,
    SlideTextElement,
    merge_stored_framing,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
PORTRAIT = (1080, 1920)
LANDSCAPE = (1920, 1080)
FIXTURE = (
    Path(__file__).resolve().parents[5] / "tests" / "fixtures" / "slide-framing-parity" / "v1.json"
)

RED, GREEN, BLUE, YELLOW = (230, 20, 20), (20, 200, 20), (20, 20, 230), (240, 230, 20)


def _near(pixel: tuple[int, ...], expected: tuple[int, int, int], tol: int = 60) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(pixel[:3], expected, strict=True))


def _quadrant_image(path: Path, size: int = 400) -> None:
    img = Image.new("RGB", (size, size))
    half = size // 2
    for box, color in (
        ((0, 0, half, half), RED),
        ((half, 0, size, half), GREEN),
        ((0, half, half, size), BLUE),
        ((half, half, size, size), YELLOW),
    ):
        img.paste(color, box)
    img.save(path)


def _slide(slide_id: str, framing: SlideFraming | None = None) -> SlideRef:
    return SlideRef(
        id=slide_id,
        asset_id="00000000-0000-0000-0000-000000000001",
        kind="image",
        edits=SlideEdits(framing=framing) if framing is not None else None,
    )


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestFramingRender:
    @pytest.mark.parametrize(
        ("crop", "expected"),
        [
            ({"x": 0, "y": 0, "width": 0.5, "height": 0.5}, RED),
            ({"x": 0.5, "y": 0, "width": 0.5, "height": 0.5}, GREEN),
            ({"x": 0, "y": 0.5, "width": 0.5, "height": 0.5}, BLUE),
            ({"x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5}, YELLOW),
        ],
    )
    def test_crop_shows_the_chosen_quadrant(self, tmp_path, crop, expected):
        src, out = tmp_path / "src.png", tmp_path / "out.jpg"
        _quadrant_image(src)
        edits = SlideEdits(framing=SlideFraming(source_crop=crop))
        normalize_image_slide(str(src), str(out), canvas=PORTRAIT, edits=edits)
        assert probe_dimensions(str(out)) == PORTRAIT
        with Image.open(out) as img:
            rgb = img.convert("RGB")
            # Away from the bottom-left watermark; both samples sit in the crop.
            assert _near(rgb.getpixel((540, 600)), expected)
            assert _near(rgb.getpixel((900, 300)), expected)

    def test_uncropped_slide_still_shows_all_quadrants(self, tmp_path):
        src, out = tmp_path / "src.png", tmp_path / "out.jpg"
        _quadrant_image(src, size=1000)
        normalize_image_slide(str(src), str(out), canvas=PORTRAIT)
        with Image.open(out) as img:
            rgb = img.convert("RGB")
            # A square cover-cropped into 9:16 keeps the middle 56% horizontally:
            # left/right of centre still hold two different colours.
            assert _near(rgb.getpixel((250, 400)), RED)
            assert _near(rgb.getpixel((830, 400)), GREEN)

    def test_landscape_slide_exports_1920x1080_and_preview_letterboxes(self, tmp_path):
        src, out, seg = tmp_path / "src.png", tmp_path / "out.jpg", tmp_path / "seg.mp4"
        _quadrant_image(src, size=800)
        edits = SlideEdits(framing=SlideFraming(orientation="landscape"))
        canvas = slide_canvas("tiktok_photo", edits)
        assert canvas == LANDSCAPE
        normalize_image_slide(str(src), str(out), canvas=canvas, edits=edits)
        assert probe_dimensions(str(out)) == LANDSCAPE
        render_preview_segment(
            str(out), str(seg), kind="image", canvas=PORTRAIT, source_canvas=canvas, hold_s=0.5
        )
        assert probe_dimensions(str(seg)) == PORTRAIT
        frame = tmp_path / "frame.png"
        import subprocess

        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(seg), "-frames:v", "1", str(frame)],
            check=True,
        )
        with Image.open(frame) as img:
            rgb = img.convert("RGB")
            assert rgb.getpixel((540, 100))[:3] == pytest.approx((0, 0, 0), abs=12)  # top bar
            assert rgb.getpixel((540, 1820))[:3] == pytest.approx((0, 0, 0), abs=12)  # bottom bar
            assert max(rgb.getpixel((540, 960))[:3]) > 100  # the slide itself

    def test_landscape_is_ignored_on_instagram(self):
        edits = SlideEdits(framing=SlideFraming(orientation="landscape"))
        assert slide_canvas("instagram_carousel", edits) == (1080, 1350)

    def test_landscape_text_is_not_stretched(self, tmp_path):
        element = SlideTextElement(id="t", text="OOOOO", size_px=120, position="center")
        portrait_png, landscape_png = tmp_path / "p.png", tmp_path / "l.png"
        render_text_element_png(element, str(portrait_png), canvas=PORTRAIT)
        render_text_element_png(element, str(landscape_png), canvas=LANDSCAPE)

        def ink_aspect(path: Path) -> float:
            with Image.open(path) as img:
                box = img.convert("RGBA").getchannel("A").point(lambda v: 255 if v > 20 else 0)
                left, top, right, bottom = box.getbbox()
            return (right - left) / (bottom - top)

        with Image.open(landscape_png) as img:
            assert img.size == LANDSCAPE
        assert ink_aspect(landscape_png) == pytest.approx(ink_aspect(portrait_png), rel=0.08)


class TestFramingContracts:
    def test_unframed_slide_cache_digest_is_unchanged(self):
        # Pinned: sha256 of the pre-framing payload `{"text":null,"look_preset":"none"}`.
        # If this moves, every existing slide derivative re-renders.
        assert edits_cache_digest(SlideEdits()) == "a268757d51d16368"
        assert edits_cache_digest(SlideEdits(look_preset="golden_hour")) == "86380fdd5a4ec3aa"

    def test_framing_changes_the_digest_and_unset_framing_is_not_serialized(self):
        framed = SlideEdits(framing=SlideFraming(orientation="landscape"))
        assert edits_cache_digest(framed) != edits_cache_digest(SlideEdits())
        assert "framing" not in SlideEdits().model_dump()
        assert "framing" not in json.loads(SlideEdits().model_dump_json())

    def test_manifest_carries_slide_dimensions(self):
        manifest = build_post_manifest(
            platform_profile="tiktok_photo",
            caption="",
            cover_index=0,
            slides=[
                BundleSlideFile("a.jpg", "image", 0, width=1920, height=1080),
                BundleSlideFile("b.jpg", "image", 1),
            ],
        )
        first, second = manifest["slides"]
        assert (first["width"], first["height"]) == (1920, 1080)
        assert "width" not in second

    def test_instagram_draft_resets_landscape_slides(self):
        landscape = SlideFraming(orientation="landscape", source_crop=None)
        draft = SlidePostDraft(
            platform_profile="instagram_carousel", slides=[_slide("a", landscape), _slide("b")]
        )
        assert draft.slides[0].edits.framing.orientation == "portrait"
        tiktok = SlidePostDraft(platform_profile="tiktok_photo", slides=[_slide("a", landscape)])
        assert tiktok.slides[0].edits.framing.orientation == "landscape"


def _stored(framing: SlideFraming | None) -> SlidePostDraft:
    return SlidePostDraft(platform_profile="tiktok_photo", slides=[_slide("a", framing)])


CROP = SlideFraming(
    orientation="landscape", source_crop={"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5}
)


class TestFramingSaveMerge:
    """An older client must never wipe a crop it cannot see."""

    def test_omitted_key_keeps_stored_framing(self):
        incoming = [
            SlideRef.model_validate({**_slide("a").model_dump(), "edits": {"look_preset": "none"}})
        ]
        out = merge_stored_framing(_stored(CROP), incoming, accept_incoming=True)
        assert out[0].edits.framing == CROP

    def test_null_edits_keeps_stored_framing(self):
        out = merge_stored_framing(_stored(CROP), [_slide("a")], accept_incoming=True)
        assert out[0].edits.framing == CROP

    def test_explicit_null_clears(self):
        incoming = [
            SlideRef.model_validate({**_slide("a").model_dump(), "edits": {"framing": None}})
        ]
        out = merge_stored_framing(_stored(CROP), incoming, accept_incoming=True)
        assert out[0].edits.framing is None

    def test_explicit_value_sets(self):
        incoming = [_slide("a", CROP)]
        out = merge_stored_framing(_stored(None), incoming, accept_incoming=True)
        assert out[0].edits.framing == CROP

    def test_flag_off_ignores_incoming_framing(self):
        out = merge_stored_framing(_stored(None), [_slide("a", CROP)], accept_incoming=False)
        assert out[0].edits is None or out[0].edits.framing is None


class TestParityFixture:
    def test_placement_matches_fixture(self):
        cases = json.loads(FIXTURE.read_text())["placements"]
        assert cases
        for case in cases:
            got = cover_placement(tuple(case["image"]), case["crop"], tuple(case["canvas"]))
            assert got == pytest.approx(tuple(case["expected"]), abs=0.01), case["name"]

    def test_focus_matches_fixture(self):
        cases = json.loads(FIXTURE.read_text())["focus"]
        for case in cases:
            got = crop_for_focus(
                case["source_aspect"], case["canvas_aspect"], case["focus"], case["zoom"]
            )
            assert got == case["expected"], case["name"]
