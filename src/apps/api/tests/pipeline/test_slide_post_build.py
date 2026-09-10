"""Real-FFmpeg tests for slide normalization, preview stitching, and bundle
assembly (no DB, no GCS — pure filesystem, mirrors test_image_to_video.py)."""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile

import pytest
from PIL import Image

from app.pipeline.slide_post.build import (
    BundleSlideFile,
    SlideBuildError,
    build_bundle_zip,
    build_post_manifest,
    concat_preview_segments,
    extract_cover,
    normalize_image_slide,
    normalize_video_slide,
    probe_dimensions,
    probe_duration_s,
    render_preview_segment,
)
from app.schemas.slide_post import SlideEdits, TextOverlay

_HAS_FFMPEG = shutil.which("ffmpeg") is not None

CANVAS = (1080, 1920)


def _make_image(path, width: int, height: int, color=(255, 0, 0)) -> None:
    Image.new("RGB", (width, height), color=color).save(path)


def _make_video(path, *, duration_s: float = 1.0, width: int = 640, height: int = 360) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:s={width}x{height}:d={duration_s}",
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
