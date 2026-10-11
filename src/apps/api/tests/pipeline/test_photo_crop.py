"""Creator crop on a still photo (KRI-44), through the real renderers.

A crop is a normalized, top-left-origin rectangle in the photo's UPRIGHT frame, so
it must pick the same region whether or not the file carries an EXIF rotation.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.pipeline.guided_story import _render_moments
from app.pipeline.image_crop import crop_image_file

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

RED, GREEN, BLUE, YELLOW = (230, 20, 20), (20, 200, 20), (20, 20, 230), (240, 230, 20)
TOP_LEFT = {"x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5}


def _near(pixel, expected, tol=60) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(pixel[:3], expected, strict=True))


def _upright_quadrants(path: Path, w: int = 40, h: int = 60) -> None:
    img = Image.new("RGB", (w, h))
    for box, color in (
        ((0, 0, w // 2, h // 2), RED),
        ((w // 2, 0, w, h // 2), GREEN),
        ((0, h // 2, w // 2, h), BLUE),
        ((w // 2, h // 2, w, h), YELLOW),
    ):
        img.paste(color, box)
    img.save(path)


def _stored_rotated_jpeg(path: Path) -> None:
    """The same upright quadrants, stored as a 60x40 raw frame with EXIF orientation 6
    (display = raw rotated 90 degrees clockwise), as an iPhone portrait photo is."""
    raw = Image.new("RGB", (60, 40))
    # Rotating raw 90 CW sends raw BL -> display TL, TL -> TR, TR -> BR, BR -> BL.
    for box, color in (
        ((0, 20, 30, 40), RED),
        ((0, 0, 30, 20), GREEN),
        ((30, 0, 60, 20), BLUE),
        ((30, 20, 60, 40), YELLOW),
    ):
        raw.paste(color, box)
    exif = Image.Exif()
    exif[0x0112] = 6
    raw.save(path, format="JPEG", quality=100, subsampling=0, exif=exif)


def test_crop_picks_the_upright_region_for_a_plain_and_an_exif_rotated_photo(tmp_path):
    plain, rotated = tmp_path / "plain.png", tmp_path / "rotated.jpg"
    _upright_quadrants(plain)
    _stored_rotated_jpeg(rotated)
    for name, source in (("plain", plain), ("rotated", rotated)):
        out = crop_image_file(str(source), TOP_LEFT, str(tmp_path / f"{name}_crop.png"))
        with Image.open(out) as img:
            assert img.size == (20, 30)
            rgb = img.convert("RGB")
            assert _near(rgb.getpixel((10, 15)), RED), name
            assert _near(rgb.getpixel((2, 2)), RED), name


def test_guided_photo_moment_renders_the_cropped_region(tmp_path):
    photo = tmp_path / "photo.png"
    _upright_quadrants(photo, 400, 600)
    moment = {
        "moment_id": "m1",
        "beat_id": "b1",
        "media_id": "photo-1",
        "generation": "1",
        "kind": "image",
        "layout": "fullscreen",
        "duration_s": 1.0,
        "look_preset": "none",
        "source_crop": TOP_LEFT,
    }
    plan = {"output_orientation": "portrait", "story_timeline": [moment]}

    outputs, receipts = _render_moments(plan, {"photo-1": str(photo)}, str(tmp_path))

    assert receipts[0]["source_crop"] == TOP_LEFT
    frame = tmp_path / "frame.png"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", outputs[0], "-frames:v", "1", str(frame)],
        check=True,
    )
    with Image.open(frame) as img:
        rgb = img.convert("RGB")
        assert rgb.size == (1080, 1920)
        # Only the red quadrant is on screen: every corner and the centre are red.
        for point in ((20, 20), (1060, 20), (540, 960), (20, 1900), (1060, 1900)):
            assert _near(rgb.getpixel(point), RED), point


def test_a_cmyk_jpeg_can_be_cropped(tmp_path):
    source = tmp_path / "print.jpg"
    Image.new("CMYK", (40, 60), (0, 255, 255, 0)).save(source, format="JPEG")
    out = crop_image_file(str(source), TOP_LEFT, str(tmp_path / "crop.png"))
    with Image.open(out) as img:
        assert img.size == (20, 30)
