"""Pasted image stickers on a slide (KRI-647 / KRI-460): geometry, decode, filters.

Pure filesystem and arithmetic, no GCS and no DB (the dispatch task downloads the
sticker originals, exactly like slide assets). The contract is frozen by
``tests/fixtures/slide-sticker-parity/v1.json`` and asserted by Python and Swift:

* the canvas is the slide's OUTPUT canvas in px (origin top-left, y down);
* ``x_frac``/``y_frac`` are the sticker CENTRE as fractions of canvas width/height;
* ``width_frac`` is the displayed width as a fraction of canvas width and the height
  follows the image's own aspect (never distorted);
* ``rotation_deg`` is clockwise-positive about the centre; corners stay transparent;
* a sticker is clipped by the canvas edge, never moved; list order is z-order.

Stacking in the render: photo -> look -> text -> drawing ink -> stickers -> watermark.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from PIL import Image

log = structlog.get_logger()

Canvas = tuple[int, int]

# Longest side of the decoded sticker raster handed to ffmpeg. A sticker is shown at
# most canvas-wide (1920 px), so keeping more than 2048 px of source is wasted memory.
STICKER_MAX_SOURCE_SIDE = 2048


class _StickerLike(Protocol):
    x_frac: float
    y_frac: float
    width_frac: float
    rotation_deg: float


@dataclass(frozen=True)
class StickerGeometry:
    """Where and how big a sticker is, in canvas pixels."""

    center_x: float
    center_y: float
    width_px: float
    height_px: float
    rotation_deg: float

    @property
    def scaled_size(self) -> tuple[int, int]:
        """Integer raster size the sticker is scaled to before it is rotated."""
        return max(1, round(self.width_px)), max(1, round(self.height_px))


def sticker_geometry(
    canvas: Canvas, image_size: tuple[int, int], sticker: _StickerLike
) -> StickerGeometry:
    """The sticker's centre, displayed size and rotation in canvas pixels."""
    canvas_w, canvas_h = canvas
    image_w, image_h = image_size
    width_px = sticker.width_frac * canvas_w
    return StickerGeometry(
        center_x=sticker.x_frac * canvas_w,
        center_y=sticker.y_frac * canvas_h,
        width_px=width_px,
        height_px=width_px * image_h / image_w,
        rotation_deg=sticker.rotation_deg,
    )


def prepare_sticker_png(src_path: str, out_png: str) -> tuple[int, int]:
    """Decode any pool image (png/webp/jpeg/HEIC) to an upright RGBA PNG; return its size.

    Alpha is never flattened: a transparent PNG stays transparent end to end. Raises on an
    unreadable file; the caller drops that sticker (best effort, like slide assets).
    """
    from PIL import Image, ImageOps  # noqa: PLC0415

    try:
        import pillow_heif  # type: ignore[import-not-found]  # noqa: PLC0415

        pillow_heif.register_heif_opener()
    except Exception:  # noqa: BLE001
        pass
    with Image.open(src_path) as opened:
        icc = opened.info.get("icc_profile")
        image = ImageOps.exif_transpose(opened)
        image.load()
        rgba = image.convert("RGBA")
    if icc:
        rgba = _to_srgb(rgba, icc)
    if max(rgba.size) > STICKER_MAX_SOURCE_SIDE:
        rgba.thumbnail((STICKER_MAX_SOURCE_SIDE, STICKER_MAX_SOURCE_SIDE), Image.LANCZOS)
    rgba.save(out_png, format="PNG")
    return rgba.size


def _to_srgb(rgba: Image.Image, icc: bytes) -> Image.Image:
    try:
        from PIL import Image, ImageCms  # noqa: PLC0415

        alpha = rgba.getchannel("A")
        converted = ImageCms.profileToProfile(
            rgba.convert("RGB"),
            ImageCms.ImageCmsProfile(io.BytesIO(icc)),
            ImageCms.createProfile("sRGB"),
        )
        out = Image.merge("RGBA", (*converted.split(), alpha))
        return out
    except Exception:  # noqa: BLE001
        return rgba


def sticker_filter_chain(geometry: StickerGeometry, *, src: str, out: str) -> str:
    """ffmpeg filters that scale ``[src]`` to the sticker's size, rotate it about its centre
    with transparent corners (``c=none``) and keep alpha (``format=rgba``) into ``[out]``."""
    width, height = geometry.scaled_size
    chain = f"[{src}]format=rgba,scale={width}:{height}:flags=lanczos"
    if abs(geometry.rotation_deg) > 1e-9:
        rad = f"{math.radians(geometry.rotation_deg):.9f}"
        chain += f",rotate={rad}:ow='rotw({rad})':oh='roth({rad})':c=none"
    return f"{chain},format=rgba[{out}]"


def sticker_overlay(geometry: StickerGeometry, *, base: str, sticker: str, out: str) -> str:
    """Overlay ``[sticker]`` on ``[base]`` with its CENTRE at the geometry's centre.

    Positions are evaluated from the (rotated) overlay's own size, so the centre is the
    pivot whatever the rotation; a negative or overflowing offset is simply clipped.
    """
    return (
        f"[{base}][{sticker}]overlay="
        f"x={geometry.center_x:.3f}-overlay_w/2:y={geometry.center_y:.3f}-overlay_h/2:"
        f"format=auto[{out}]"
    )
