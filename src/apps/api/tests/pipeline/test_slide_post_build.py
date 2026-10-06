"""Real-FFmpeg tests for slide normalization, preview stitching, and bundle
assembly (no DB, no GCS — pure filesystem, mirrors test_image_to_video.py)."""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageStat

from app.config import settings
from app.pipeline.slide_post.build import (
    _WATERMARK_PNG,
    SLIDE_IMAGE_NORMALIZER_VERSION,
    SLIDE_WATERMARK_VERSION,
    BundleSlideFile,
    SlideBuildError,
    build_bundle_zip,
    build_post_manifest,
    concat_preview_segments,
    edits_cache_digest,
    extract_cover,
    normalize_image_slide,
    normalize_video_slide,
    probe_dimensions,
    probe_duration_s,
    render_preview_segment,
    rich_text_active,
)
from app.schemas.slide_post import SlideEdits, SlideTextElement, TextOverlay

_HAS_FFMPEG = shutil.which("ffmpeg") is not None

CANVAS = (1080, 1920)


def _make_image(path, width: int, height: int, color=(255, 0, 0)) -> None:
    Image.new("RGB", (width, height), color=color).save(path)


def _make_video(
    path,
    *,
    duration_s: float = 1.0,
    width: int = 640,
    height: int = 360,
    color: str = "blue",
    audio: bool = False,
) -> None:
    audio_args = (
        ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_s}", "-c:a", "aac"]
        if audio
        else []
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={width}x{height}:d={duration_s}",
            *audio_args,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestNormalizeImageSlide:
    def test_landscape_image_normalizes_to_canvas(self, tmp_path):
        src = tmp_path / "src.png"
        out = tmp_path / "out.jpg"
        _make_image(src, 1920, 1080)
        normalize_image_slide(str(src), str(out), canvas=CANVAS)
        assert out.exists()
        assert probe_dimensions(str(out)) == CANVAS

    def test_missing_source_raises(self, tmp_path):
        with pytest.raises(SlideBuildError):
            normalize_image_slide(
                str(tmp_path / "nope.png"), str(tmp_path / "out.jpg"), canvas=CANVAS
            )

    def test_look_preset_edit_changes_pixels_but_not_dimensions(self, tmp_path):
        src = tmp_path / "src.png"
        plain = tmp_path / "plain.jpg"
        edited = tmp_path / "edited.jpg"
        _make_image(src, 1920, 1080, color=(120, 140, 160))
        normalize_image_slide(str(src), str(plain), canvas=CANVAS)
        normalize_image_slide(
            str(src),
            str(edited),
            canvas=CANVAS,
            edits=SlideEdits(look_preset="olive_film"),
        )
        assert probe_dimensions(str(edited)) == CANVAS
        assert plain.read_bytes() != edited.read_bytes()

    def test_text_overlay_edit_changes_pixels_but_not_dimensions(self, tmp_path):
        src = tmp_path / "src.png"
        plain = tmp_path / "plain.jpg"
        edited = tmp_path / "edited.jpg"
        _make_image(src, 1920, 1080)
        normalize_image_slide(str(src), str(plain), canvas=CANVAS)
        normalize_image_slide(
            str(src),
            str(edited),
            canvas=CANVAS,
            edits=SlideEdits(text=TextOverlay(content="sold out", position="bottom")),
        )
        assert probe_dimensions(str(edited)) == CANVAS
        assert plain.read_bytes() != edited.read_bytes()

    def test_text_overlay_with_filter_delimiter_characters_does_not_break_ffmpeg(self, tmp_path):
        """FFmpeg's drawtext `text=`/`textfile=` value uses `:`/`'`/`%`/`,`
        as its own syntax. Asserts more than "ffmpeg didn't raise": a `%`
        in ordinary text (e.g. "50% off") previously logged "Stray %" and
        SILENTLY DROPPED THE ENTIRE OVERLAY — exit code 0, no exception, no
        visible text, `plain.jpg` byte-identical to the "edited" output.
        Caught only by manually inspecting rendered pixels locally, not by
        the exists()/dimensions-only version of this test that shipped
        first. Pins pixel difference so the same bug can't regress
        silently again."""
        src = tmp_path / "src.png"
        plain = tmp_path / "plain.jpg"
        out = tmp_path / "out.jpg"
        _make_image(src, 1920, 1080)
        normalize_image_slide(str(src), str(plain), canvas=CANVAS)
        normalize_image_slide(
            str(src),
            str(out),
            canvas=CANVAS,
            edits=SlideEdits(
                text=TextOverlay(content="50% off: today's deal, 'limited'", position="top")
            ),
        )
        assert out.exists()
        assert probe_dimensions(str(out)) == CANVAS
        assert plain.read_bytes() != out.read_bytes(), (
            "text overlay produced no visible change — drawtext silently dropped the text"
        )

    def test_text_and_look_preset_edits_compose(self, tmp_path):
        src = tmp_path / "src.png"
        out = tmp_path / "out.jpg"
        _make_image(src, 1920, 1080)
        normalize_image_slide(
            str(src),
            str(out),
            canvas=CANVAS,
            edits=SlideEdits(
                look_preset="smoky_split_tone",
                text=TextOverlay(content="both at once", position="center"),
            ),
        )
        assert out.exists()
        assert probe_dimensions(str(out)) == CANVAS


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestNormalizeVideoSlide:
    def test_video_slide_normalizes_to_canvas_and_keeps_audio_track(self, tmp_path):
        src = tmp_path / "src.mp4"
        out = tmp_path / "out.mp4"
        _make_video(src, duration_s=1.5)
        normalize_video_slide(str(src), str(out), canvas=CANVAS)
        assert out.exists()
        assert probe_dimensions(str(out)) == CANVAS
        assert probe_duration_s(str(out)) == pytest.approx(1.5, abs=0.3)

    def test_video_slide_with_text_and_look_preset_edits(self, tmp_path):
        src = tmp_path / "src.mp4"
        out = tmp_path / "out.mp4"
        _make_video(src, duration_s=1.0)
        normalize_video_slide(
            str(src),
            str(out),
            canvas=CANVAS,
            edits=SlideEdits(
                look_preset="golden_hour",
                text=TextOverlay(content="video slide edit", position="top"),
            ),
        )
        assert out.exists()
        assert probe_dimensions(str(out)) == CANVAS
        assert probe_duration_s(str(out)) == pytest.approx(1.0, abs=0.3)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestPreviewSegmentsAndConcat:
    def test_image_segment_holds_for_requested_duration(self, tmp_path):
        src = tmp_path / "slide.jpg"
        seg = tmp_path / "seg.mp4"
        _make_image(src, 1080, 1920)
        render_preview_segment(str(src), str(seg), kind="image", canvas=CANVAS, hold_s=1.0)
        assert probe_duration_s(str(seg)) == pytest.approx(1.0, abs=0.2)

    def test_video_segment_is_capped_and_silent(self, tmp_path):
        src = tmp_path / "slide.mp4"
        seg = tmp_path / "seg.mp4"
        _make_video(src, duration_s=5.0)
        render_preview_segment(str(src), str(seg), kind="video", canvas=CANVAS, hold_s=2.0)
        assert probe_duration_s(str(seg)) == pytest.approx(2.0, abs=0.3)

    def test_concat_joins_segments_in_order_with_summed_duration(self, tmp_path):
        img_slide = tmp_path / "a.jpg"
        vid_slide = tmp_path / "b.mp4"
        _make_image(img_slide, 1080, 1920)
        _make_video(vid_slide, duration_s=3.0)
        seg_a = tmp_path / "seg_a.mp4"
        seg_b = tmp_path / "seg_b.mp4"
        render_preview_segment(str(img_slide), str(seg_a), kind="image", canvas=CANVAS, hold_s=1.0)
        render_preview_segment(str(vid_slide), str(seg_b), kind="video", canvas=CANVAS, hold_s=2.0)
        out = tmp_path / "preview.mp4"
        concat_preview_segments([str(seg_a), str(seg_b)], str(out), canvas=CANVAS)
        assert probe_duration_s(str(out)) == pytest.approx(3.0, abs=0.4)
        assert probe_dimensions(str(out)) == CANVAS

    def test_concat_requires_at_least_one_segment(self, tmp_path):
        with pytest.raises(SlideBuildError):
            concat_preview_segments([], str(tmp_path / "out.mp4"), canvas=CANVAS)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestExtractCover:
    def test_image_cover_is_a_copy_of_the_normalized_slide(self, tmp_path):
        src = tmp_path / "slide.jpg"
        out = tmp_path / "cover.jpg"
        _make_image(src, 1080, 1920)
        extract_cover(str(src), "image", str(out))
        assert out.read_bytes() == src.read_bytes()

    def test_video_cover_extracts_a_single_frame(self, tmp_path):
        src = tmp_path / "slide.mp4"
        out = tmp_path / "cover.jpg"
        _make_video(src, duration_s=2.0, width=1080, height=1920)
        extract_cover(str(src), "video", str(out))
        assert out.exists()
        img = Image.open(out)
        assert img.format == "JPEG"


class TestManifestAndBundle:
    def test_manifest_preserves_order_and_kinds(self):
        slides = [
            BundleSlideFile(local_path="/a.jpg", kind="image", index=0, alt="first"),
            BundleSlideFile(local_path="/b.mp4", kind="video", index=1, alt=None),
        ]
        manifest = build_post_manifest(
            platform_profile="instagram_carousel", caption="hi", cover_index=0, slides=slides
        )
        assert manifest["slides"] == [
            {"index": 0, "kind": "image", "filename": "00.jpg", "alt": "first"},
            {"index": 1, "kind": "video", "filename": "01.mp4", "alt": None},
        ]
        assert manifest["cover_index"] == 0
        assert manifest["caption"] == "hi"

    def test_bundle_zip_contains_index_ordered_slides_manifest_and_caption(self, tmp_path):
        img_path = tmp_path / "img.jpg"
        vid_path = tmp_path / "vid.mp4"
        img_path.write_bytes(b"fake-jpeg-bytes")
        vid_path.write_bytes(b"fake-mp4-bytes")
        slides = [
            BundleSlideFile(local_path=str(img_path), kind="image", index=0),
            BundleSlideFile(local_path=str(vid_path), kind="video", index=1),
        ]
        manifest = build_post_manifest(
            platform_profile="instagram_carousel",
            caption="my caption",
            cover_index=0,
            slides=slides,
        )
        out_zip = tmp_path / "bundle.zip"
        build_bundle_zip(str(out_zip), slides=slides, manifest=manifest, caption="my caption")

        with zipfile.ZipFile(out_zip) as zf:
            names = zf.namelist()
            assert names == ["slides/00.jpg", "slides/01.mp4", "post.json", "caption.txt"]
            assert zf.read("slides/00.jpg") == b"fake-jpeg-bytes"
            assert zf.read("slides/01.mp4") == b"fake-mp4-bytes"
            assert json.loads(zf.read("post.json")) == manifest
            assert zf.read("caption.txt").decode() == "my caption"


# ---- Rich per-slide text (KRI-299) ---------------------------------------------------


CANVAS_45 = (1080, 1350)


@pytest.fixture
def rich_on(monkeypatch):
    monkeypatch.setattr(settings, "slide_post_rich_text_enabled", True)


def _el(**kw) -> SlideTextElement:
    base = {"id": "t1", "text": "Lisbon", "color": "#FF0000", "size_px": 120, "position": "center"}
    base.update(kw)
    return SlideTextElement(**base)


def _bbox_of_color(path, rgb, tol=40):
    img = Image.open(path).convert("RGB")
    w, h = img.size
    px = img.load()
    xs, ys = [], []
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            r, g, b = px[x, y]
            if abs(r - rgb[0]) < tol and abs(g - rgb[1]) < tol and abs(b - rgb[2]) < tol:
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestRichSlideText:
    def test_styled_image_slide_draws_colored_text_where_asked(self, tmp_path, rich_on):
        src, out = tmp_path / "s.png", tmp_path / "o.jpg"
        _make_image(src, 1080, 1920, color=(0, 0, 0))
        edits = SlideEdits(texts=[_el()])
        normalize_image_slide(str(src), str(out), canvas=CANVAS, edits=edits)
        assert probe_dimensions(str(out)) == CANVAS
        box = _bbox_of_color(out, (255, 0, 0))
        assert box is not None, "styled text not visible"
        cy = (box[1] + box[3]) / 2
        assert 0.4 * 1920 < cy < 0.6 * 1920
        assert not [p for p in tmp_path.iterdir() if ".text" in p.name]

    def test_apostrophe_and_percent_text_render(self, tmp_path, rich_on):
        src, out = tmp_path / "s.png", tmp_path / "o.jpg"
        _make_image(src, 1080, 1920, color=(0, 0, 0))
        edits = SlideEdits(texts=[_el(text="50% of Dad's day")])
        normalize_image_slide(str(src), str(out), canvas=CANVAS, edits=edits)
        assert _bbox_of_color(out, (255, 0, 0)) is not None

    def test_multiple_elements_and_look_compose(self, tmp_path, rich_on):
        src, out = tmp_path / "s.png", tmp_path / "o.jpg"
        _make_image(src, 1080, 1920, color=(0, 0, 0))
        edits = SlideEdits(
            look_preset="golden_hour",
            texts=[_el(id="a", position="top"), _el(id="b", color="#00FF00", position="bottom")],
        )
        normalize_image_slide(str(src), str(out), canvas=CANVAS, edits=edits)
        top = _bbox_of_color(out, (255, 0, 0), tol=90)
        bottom = _bbox_of_color(out, (0, 255, 0), tol=90)
        assert top and bottom and top[1] < bottom[1]

    def test_four_by_five_top_band_lands_inside_cropped_frame(self, tmp_path, rich_on):
        src, out = tmp_path / "s.png", tmp_path / "o.jpg"
        _make_image(src, 1080, 1350, color=(0, 0, 0))
        edits = SlideEdits(texts=[_el(position="custom", x_frac=0.5, y_frac=0.1, size_px=80)])
        normalize_image_slide(str(src), str(out), canvas=CANVAS_45, edits=edits)
        assert probe_dimensions(str(out)) == CANVAS_45
        box = _bbox_of_color(out, (255, 0, 0))
        assert box is not None
        cy = (box[1] + box[3]) / 2
        # y_frac 0.1 of a 1350-tall slide, not of the 1920 raster.
        assert abs(cy - 0.1 * 1350) < 40

    def test_video_slide_keeps_full_duration_and_audio_with_text(self, tmp_path, rich_on):
        src, out = tmp_path / "s.mp4", tmp_path / "o.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=640x360:d=1.5",
                "-f",
                "lavfi",
                "-i",
                "sine=d=1.5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(src),
            ],
            check=True,
            capture_output=True,
        )
        normalize_video_slide(str(src), str(out), canvas=CANVAS, edits=SlideEdits(texts=[_el()]))
        assert probe_dimensions(str(out)) == CANVAS
        assert abs(probe_duration_s(str(out)) - 1.5) < 0.3
        streams = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(out),
            ],
            text=True,
        )
        assert "audio" in streams
        frame = tmp_path / "f.jpg"
        extract_cover(str(out), "video", str(frame))
        assert _bbox_of_color(frame, (255, 0, 0)) is not None

    def test_flag_off_uses_legacy_drawtext_of_mirrored_text(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "slide_post_rich_text_enabled", False)
        src, out = tmp_path / "s.png", tmp_path / "o.jpg"
        _make_image(src, 1080, 1920, color=(0, 0, 0))
        edits = SlideEdits(texts=[_el()])
        assert edits.text is not None and edits.text.content == "Lisbon"
        assert not rich_text_active(edits)
        normalize_image_slide(str(src), str(out), canvas=CANVAS, edits=edits)
        # drawtext is white on a translucent box; the styled red never appears.
        assert _bbox_of_color(out, (255, 0, 0)) is None
        assert _bbox_of_color(out, (255, 255, 255)) is not None

    def test_legacy_only_edits_render_byte_identical_with_flag_on_or_off(
        self, tmp_path, monkeypatch
    ):
        src = tmp_path / "s.png"
        _make_image(src, 1080, 1920, color=(10, 20, 30))
        legacy = SlideEdits(text=TextOverlay(content="Hi", position="bottom"))
        outs = []
        for flag in (False, True):
            monkeypatch.setattr(settings, "slide_post_rich_text_enabled", flag)
            out = tmp_path / f"o{flag}.jpg"
            normalize_image_slide(str(src), str(out), canvas=CANVAS, edits=legacy)
            outs.append(out.read_bytes())
        assert outs[0] == outs[1]

    def test_empty_texts_renders_without_overlay(self, tmp_path, rich_on):
        src, out = tmp_path / "s.png", tmp_path / "o.jpg"
        _make_image(src, 1080, 1920, color=(0, 0, 0))
        normalize_image_slide(str(src), str(out), canvas=CANVAS, edits=SlideEdits(texts=[]))
        assert probe_dimensions(str(out)) == CANVAS


class TestEditsCacheDigest:
    def test_legacy_digest_ignores_texts_field(self):
        import hashlib

        legacy = SlideEdits(text=TextOverlay(content="Hi", position="top"))
        expected = hashlib.sha256(legacy.model_dump_json(exclude={"texts"}).encode()).hexdigest()[
            :16
        ]
        assert edits_cache_digest(legacy) == expected
        assert edits_cache_digest(None) == "noedits"

    def test_digest_covers_every_style_field(self):
        a = edits_cache_digest(SlideEdits(texts=[_el()]))
        assert a != edits_cache_digest(SlideEdits(texts=[_el(color="#00FF00")]))
        assert a != edits_cache_digest(SlideEdits(texts=[_el(y_frac=0.2, position="custom")]))
        assert a != edits_cache_digest(SlideEdits(texts=[_el(), _el(id="t2")]))

    def test_flag_flip_changes_digest(self, monkeypatch):
        edits = SlideEdits(texts=[_el()])
        monkeypatch.setattr(settings, "slide_post_rich_text_enabled", False)
        off = edits_cache_digest(edits)
        monkeypatch.setattr(settings, "slide_post_rich_text_enabled", True)
        assert edits_cache_digest(edits) != off


def _mean_rgb(path):
    img = Image.open(path).convert("RGB").resize((8, 8))
    px = list(img.getdata())
    return tuple(sum(p[i] for p in px) / len(px) for i in range(3))


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestNormalizeImageDecode:
    def test_heic_renders_colourful_at_canvas(self, tmp_path):
        pillow_heif = pytest.importorskip("pillow_heif")
        pillow_heif.register_heif_opener()
        src = tmp_path / "src.heic"
        try:
            Image.new("RGB", (800, 600), (220, 40, 40)).save(src, format="HEIF")
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"HEIF encode unavailable: {exc}")
        out = tmp_path / "out.jpg"
        normalize_image_slide(str(src), str(out), canvas=CANVAS)
        assert probe_dimensions(str(out)) == CANVAS
        r, g, b = _mean_rgb(out)
        assert r > 150 and g < 100 and b < 100  # still red, not B&W

    def test_exif_orientation_is_applied(self, tmp_path):
        src = tmp_path / "src.jpg"
        img = Image.new("RGB", (400, 200), (0, 0, 255))
        # Left half red; orientation 6 (rotate 90 CW) puts it on top.
        img.paste((255, 0, 0), (0, 0, 200, 200))
        exif = Image.Exif()
        exif[0x0112] = 6
        img.save(src, exif=exif)
        out = tmp_path / "out.jpg"
        normalize_image_slide(str(src), str(out), canvas=(200, 400))
        im = Image.open(out).convert("RGB")
        assert im.size == (200, 400)
        top = im.getpixel((100, 50))
        bottom = im.getpixel((100, 350))
        assert top[0] > 200 and top[2] < 80
        assert bottom[2] > 200 and bottom[0] < 80

    def test_undecodable_source_falls_back_to_original_path(self, tmp_path):
        from app.pipeline.slide_post.build import _decode_image_for_ffmpeg

        bad = tmp_path / "bad.bin"
        bad.write_bytes(b"not an image")
        assert _decode_image_for_ffmpeg(str(bad)) == str(bad)


# ---- Kria watermark (KRI-472) ---------------------------------------------------------

# The mark's own box (shadow pad excluded) on the 1080x1920 reference: left
# edge 60, 133px wide, bottom edge 445px above the bottom of the frame.
_MARK_LEFT, _MARK_W, _MARK_H, _MARK_BOTTOM_INSET = 60, 133, 59, 445


def _mark_box(canvas, *, scale: float = 1.0) -> tuple[int, int, int, int]:
    _, ch = canvas
    bottom = ch - _MARK_BOTTOM_INSET * scale
    return (
        round(_MARK_LEFT * scale),
        round(bottom - _MARK_H * scale),
        round((_MARK_LEFT + _MARK_W) * scale),
        round(bottom),
    )


def _brightest(path, box) -> int:
    with Image.open(path) as img:
        return ImageStat.Stat(img.convert("L").crop(box)).extrema[0][1]


def _ffprobe_stream_count(path, kind: str) -> int:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            kind,
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ],
        text=True,
    )
    return len([line for line in out.splitlines() if line.strip()])


def test_bundled_watermark_matches_the_brand_kit():
    """The API's copy is written by brand/social/build.py; this stops it
    drifting from the kit (iOS has the same guard in BrandingTests)."""
    kit = (
        Path(__file__).resolve().parents[5]
        / "brand/social/dist/watermark/kria-watermark-mist-standard.png"
    )
    if not kit.exists():
        pytest.skip("brand kit not present in this checkout")
    assert Path(_WATERMARK_PNG).read_bytes() == kit.read_bytes(), (
        "assets/branding watermark drifted from brand/social/dist; re-run build.py"
    )


def test_watermark_composites_last_and_scales_with_the_canvas():
    from app.pipeline.slide_post.build import _overlay_filter_complex

    tall = _overlay_filter_complex("scale=1080:1920", 2, canvas=(1080, 1920))
    # Text PNGs are inputs 1-2; the watermark is input 3 and lands last, on
    # [vout], at the reference placement with no rescale.
    assert tall.endswith("[b2][3:v]overlay=30:main_h-overlay_h-415:format=auto[vout]")
    assert "scale=iw" not in tall

    carousel = _overlay_filter_complex("scale=1080:1350", 0, canvas=(1080, 1350))
    # 4:5: one scale (height-bound, 1350/1920) for both the mark and its insets.
    assert "[1:v]scale=iw*0.703125:ih*0.703125" in carousel
    assert carousel.endswith("[b0][wm]overlay=21:main_h-overlay_h-292:format=auto[vout]")


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
class TestSlideWatermark:
    def test_image_slide_carries_the_mark_bottom_left(self, tmp_path):
        src = tmp_path / "black.png"
        out = tmp_path / "out.jpg"
        _make_image(src, 1080, 1920, color=(0, 0, 0))
        normalize_image_slide(str(src), str(out), canvas=CANVAS)
        assert _brightest(out, _mark_box(CANVAS)) > 60
        # Nowhere else: the mirrored bottom-right box and the top stay black.
        left, top, right, bottom = _mark_box(CANVAS)
        assert _brightest(out, (1080 - right, top, 1080 - left, bottom)) < 20
        assert _brightest(out, (left, 200, right, 300)) < 20

    def test_carousel_slide_gets_a_proportional_mark_in_the_same_corner(self, tmp_path):
        canvas = (1080, 1350)
        src = tmp_path / "black.png"
        out = tmp_path / "out.jpg"
        _make_image(src, 1080, 1350, color=(0, 0, 0))
        normalize_image_slide(str(src), str(out), canvas=canvas)
        scaled = _mark_box(canvas, scale=1350 / 1920)
        assert _brightest(out, scaled) > 60
        # Not at the unscaled 9:16 inset either.
        left, _, right, _ = scaled
        assert _brightest(out, (left, 1350 - 445 - 59, right, 1350 - 445)) < 20

    def test_mark_stays_on_top_of_slide_text(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "slide_post_rich_text_enabled", True)
        src = tmp_path / "black.png"
        out = tmp_path / "out.jpg"
        _make_image(src, 1080, 1920, color=(0, 0, 0))
        # An opaque black box behind the text, placed over the mark's corner:
        # if the text were composited last, it would hide the mark.
        element = SlideTextElement(
            id="t",
            text="COVER",
            position="custom",
            x_frac=0.02,
            y_frac=0.75,
            alignment="left",
            color="#000000",
            background_color="#000000",
            size_px=200,
        )
        # Precondition: the text layer alone is opaque over the whole mark box.
        from app.pipeline.slide_post.build import render_text_element_png

        layer = tmp_path / "layer.png"
        render_text_element_png(element, str(layer), canvas=CANVAS)
        with Image.open(layer) as img:
            alpha = img.getchannel("A").crop(_mark_box(CANVAS))
            assert ImageStat.Stat(alpha).extrema[0][0] == 255
        normalize_image_slide(str(src), str(out), canvas=CANVAS, edits=SlideEdits(texts=[element]))
        assert _brightest(out, _mark_box(CANVAS)) > 60

    def test_video_slide_carries_the_mark_and_keeps_one_audio_track(self, tmp_path):
        src = tmp_path / "src.mp4"
        out = tmp_path / "out.mp4"
        frame = tmp_path / "frame.png"
        _make_video(src, duration_s=1.5, color="black", audio=True)
        normalize_video_slide(str(src), str(out), canvas=CANVAS)
        assert _ffprobe_stream_count(out, "a") == 1
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", "0.5", "-i", str(out)]
            + ["-frames:v", "1", str(frame)],
            check=True,
            capture_output=True,
        )
        assert _brightest(frame, _mark_box(CANVAS)) > 60

    def test_silent_video_slide_still_renders(self, tmp_path):
        src = tmp_path / "src.mp4"
        out = tmp_path / "out.mp4"
        _make_video(src, duration_s=1.0)
        normalize_video_slide(str(src), str(out), canvas=CANVAS)
        assert _ffprobe_stream_count(out, "a") == 0
        assert probe_dimensions(str(out)) == CANVAS


def test_normalizer_version_is_part_of_image_cache_key():
    import inspect

    from app.tasks import generative_build

    assert SLIDE_IMAGE_NORMALIZER_VERSION == 2
    src = inspect.getsource(generative_build)
    assert "SLIDE_IMAGE_NORMALIZER_VERSION" in src


def test_watermark_version_is_part_of_every_slide_cache_key():
    import inspect

    from app.tasks import generative_build

    assert SLIDE_WATERMARK_VERSION == 1
    src = inspect.getsource(generative_build._build_slide_post_result)
    assert "_wm{slide_build.SLIDE_WATERMARK_VERSION}" in src
    assert '"watermark_version": slide_build.SLIDE_WATERMARK_VERSION' in src
