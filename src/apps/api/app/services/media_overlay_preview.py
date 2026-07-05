"""Preview generation for browser-hostile media overlay images."""

from __future__ import annotations

import os
import tempfile

import structlog

from app import storage

log = structlog.get_logger()


def nonblank_str(value: object) -> str | None:
    """Return a stripped string, or None for missing/blank values."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def is_heif_overlay(path: str, content_type: str = "") -> bool:
    ext = os.path.splitext(path)[1].lower()
    return content_type in {"image/heic", "image/heif"} or ext in {".heic", ".heif"}


def convert_heif_overlay_preview(gcs_path: str) -> tuple[str | None, str | None]:
    try:
        import pillow_heif  # type: ignore[import]  # noqa: PLC0415
        from PIL import Image, ImageOps  # noqa: PLC0415

        pillow_heif.register_heif_opener()
        with tempfile.TemporaryDirectory(prefix="nova_overlay_preview_") as tmpdir:
            raw_path = os.path.join(tmpdir, "overlay")
            preview_path = os.path.join(tmpdir, "preview.jpg")
            storage.download_to_file(gcs_path, raw_path)
            with Image.open(raw_path) as img:
                ImageOps.exif_transpose(img).convert("RGB").save(
                    preview_path,
                    format="JPEG",
                    quality=92,
                    optimize=True,
                )
            preview_gcs_path = f"{gcs_path}.preview.jpg"
            preview_url = storage.upload_public_read(
                preview_path,
                preview_gcs_path,
                content_type="image/jpeg",
            )
            return preview_gcs_path, preview_url
    except Exception as exc:  # noqa: BLE001
        log.error(
            "overlay_heif_preview_convert_failed",
            gcs_path=gcs_path,
            error=str(exc),
            exc_info=True,
        )
        return None, None
