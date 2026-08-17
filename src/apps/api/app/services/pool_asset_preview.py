"""Preview generation for browser-hostile visuals-pool uploads.

The pool (plan 005) never had a preview pipeline: `_asset_out` in
`app/routes/plan_items.py` used to sign the RAW uploaded object as
`display_url`. iPhone HEIC photos and HEVC-in-QuickTime .mov videos can't be
decoded by Chromium, so `ready` assets rendered blank in the editor. This
mirrors `app/services/media_overlay_preview.py`'s HEIC-preview approach
(same PIL + pillow_heif recipe) and extends it to video posters, since ANY
video codec benefits from a poster frame and some codecs can't play in the
browser at all.

PIL/pillow_heif are imported lazily inside functions — the structural eval CI
job has no libEGL/skia and this module must stay import-light so it can be
imported there without pulling in heavy optional deps. FFmpeg runs via
`subprocess.run` directly — never MoviePy (buffers the whole file into RAM).

Both generator functions are best-effort: any failure is logged and returns
False, never raises. A failed preview must never fail the asset's analysis —
the source asset still goes `ready`; the caller persists `""` as a
do-not-retry sentinel for the fast paths.
"""

from __future__ import annotations

import os
import subprocess

import structlog

log = structlog.get_logger()

_HEIF_CONTENT_TYPES = {"image/heic", "image/heif"}
_HEIF_EXTENSIONS = {".heic", ".heif"}
_POSTER_TIMEOUT_S = 60


def preview_object_path(gcs_path: str) -> str:
    """Sibling object key for a preview, alongside the raw upload.

    Pool assets live under the persistent `users/{uid}/plan/{item}/pool/`
    prefix (not the 24h-lifecycle `dev-user/*` prefix) — a sibling key
    inherits that persistence, so no bucket lifecycle rule sweeps it out from
    under a `ready` asset.
    """
    return f"{gcs_path}.preview.jpg"


def needs_preview(kind: str, content_type: str | None, gcs_path: str) -> bool:
    """Whether this asset needs a browser-safe preview generated.

    True for every video — a poster frame helps regardless of codec, and
    HEVC-in-.mov can't be decoded by Chromium at all. True for HEIC/HEIF
    images (same decode gap, detected by content-type or file extension).
    False for images already browser-safe (JPEG/PNG/WebP).
    """
    if kind == "video":
        return True
    ext = os.path.splitext(gcs_path)[1].lower()
    ct = (content_type or "").strip().lower()
    return ct in _HEIF_CONTENT_TYPES or ext in _HEIF_EXTENSIONS


def write_image_preview(src: str, dst: str) -> bool:
    """Decode a (possibly HEIC/HEIF) image and write a browser-safe JPEG.

    Never raises. Returns False (and logs a warning) on any failure.
    """
    try:
        import pillow_heif  # type: ignore[import]  # noqa: PLC0415
        from PIL import Image, ImageOps  # noqa: PLC0415

        pillow_heif.register_heif_opener()
        with Image.open(src) as img:
            oriented = ImageOps.exif_transpose(img).convert("RGB")
            oriented.thumbnail((720, 720))
            oriented.save(dst, format="JPEG", quality=85)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("pool_asset_image_preview_failed", src=src, error=str(exc))
        return False


def write_video_poster(src: str, dst: str) -> bool:
    """Extract a single poster frame as a browser-safe JPEG via ffmpeg.

    Never raises. Returns False (and logs a warning) on any nonzero exit or
    timeout.
    """
    try:
        result = subprocess.run(  # noqa: S603
            [
                "ffmpeg",
                "-y",
                "-ss",
                "0.5",
                "-i",
                src,
                "-frames:v",
                "1",
                "-vf",
                "scale='min(720,iw)':-2",
                dst,
            ],
            capture_output=True,
            timeout=_POSTER_TIMEOUT_S,
        )
        if result.returncode != 0:
            stderr = result.stderr[-500:] if result.stderr else None
            log.warning(
                "pool_asset_video_poster_failed",
                src=src,
                returncode=result.returncode,
                stderr=stderr,
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        log.warning("pool_asset_video_poster_timed_out", src=src)
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("pool_asset_video_poster_failed", src=src, error=str(exc))
        return False
