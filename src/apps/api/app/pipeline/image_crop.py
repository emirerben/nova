"""Crop a still to a normalized upright-space rectangle (KRI-44).

Photos carry EXIF orientation that ffmpeg and Pillow treat differently, so the
crop is always taken from the upright (display) pixels -- the same space the
iOS crop editor and the video ``reframe`` crop use -- and written as a plain
upright PNG, so later renders never re-apply the orientation.
"""

from __future__ import annotations

from collections.abc import Mapping


def crop_image_file(source: str, crop: Mapping[str, float], out_path: str) -> str:
    """Write the ``crop`` region (normalized, top-left origin) of ``source`` to ``out_path``."""
    from PIL import Image, ImageOps  # noqa: PLC0415

    try:
        import pillow_heif  # type: ignore[import-not-found]  # noqa: PLC0415

        pillow_heif.register_heif_opener()
    except Exception:  # noqa: BLE001 - HEIC support is best effort
        pass

    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        width, height = image.size
        left = min(width - 1, max(0, round(crop["x"] * width)))
        top = min(height - 1, max(0, round(crop["y"] * height)))
        right = max(left + 1, min(width, round((crop["x"] + crop["width"]) * width)))
        bottom = max(top + 1, min(height, round((crop["y"] + crop["height"]) * height)))
        image.crop((left, top, right, bottom)).save(out_path, format="PNG")
    return out_path
