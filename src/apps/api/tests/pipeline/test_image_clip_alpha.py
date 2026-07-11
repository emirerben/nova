from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from PIL import Image

from app.pipeline import image_clip


def test_image_has_alpha_true_for_non_opaque_rgba(tmp_path: Path) -> None:
    path = tmp_path / "transparent.png"
    im = Image.new("RGBA", (2, 2), (255, 0, 0, 255))
    im.putpixel((0, 0), (255, 0, 0, 80))
    im.save(path)

    assert image_clip.image_has_alpha(str(path)) is True


def test_image_has_alpha_false_for_opaque_rgba(tmp_path: Path) -> None:
    path = tmp_path / "opaque.png"
    Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(path)

    assert image_clip.image_has_alpha(str(path)) is False


def test_image_has_alpha_false_for_rgb_jpeg(tmp_path: Path) -> None:
    path = tmp_path / "plain.jpg"
    Image.new("RGB", (2, 2), (255, 0, 0)).save(path)

    assert image_clip.image_has_alpha(str(path)) is False


def test_image_has_alpha_false_for_oversized_image(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "huge.png"
    path.write_bytes(b"fake")

    class _HugeImage:
        width = 5001
        height = 5000
        info = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getbands(self):
            return ("R", "G", "B", "A")

    monkeypatch.setattr(Image, "open", lambda _path: _HugeImage())

    assert image_clip.image_has_alpha(str(path)) is False


def test_image_has_alpha_true_for_palette_png_transparency(tmp_path: Path) -> None:
    path = tmp_path / "palette.png"
    im = Image.new("P", (2, 2))
    im.putpalette([255, 0, 0, 0, 255, 0] + [0, 0, 0] * 254)
    im.putdata([0, 1, 1, 1])
    im.info["transparency"] = bytes([0, 255] + [255] * 254)
    im.save(path)

    assert image_clip.image_has_alpha(str(path)) is True


def test_normalize_to_png_passthrough_for_png(tmp_path: Path) -> None:
    path = tmp_path / "card.png"
    out = tmp_path / "out.png"
    Image.new("RGBA", (2, 2), (255, 0, 0, 80)).save(path)

    assert image_clip.normalize_to_png(str(path), str(out)) == (str(path), True)
    assert not out.exists()


def test_normalize_to_png_failure_falls_back_to_jpeg_flatten(monkeypatch, tmp_path: Path) -> None:
    src = tmp_path / "card.webp"
    out = tmp_path / "card.png"
    src.write_bytes(b"webp")
    fallback = tmp_path / "card.jpg"

    monkeypatch.setattr(
        image_clip.subprocess,
        "run",
        lambda *_args, **_kwargs: MagicMock(returncode=1, stderr=b"nope"),
    )

    def fake_jpeg(input_path: str, output_path: str) -> None:
        assert input_path == str(src)
        assert output_path == str(fallback)
        Path(output_path).write_bytes(b"jpg")

    monkeypatch.setattr(image_clip, "normalize_to_jpeg", fake_jpeg)

    assert image_clip.normalize_to_png(str(src), str(out)) == (str(fallback), False)
    assert fallback.read_bytes() == b"jpg"
