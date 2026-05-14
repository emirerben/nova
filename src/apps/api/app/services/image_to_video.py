"""Convert a still image into a 9:16 looping mp4 for use as a template clip.

Pipeline:
  1. Open with PIL (HEIC via pillow-heif).
  2. Apply EXIF rotation (iPhone images often carry orientation tags).
  3. Center-crop / pad to 1080x1920 (9:16 portrait) preserving content.
  4. Encode as a static mp4 of `duration_s` seconds via FFmpeg `-loop 1`.
"""

from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path

import structlog
from PIL import Image, ImageOps

# Register HEIC/HEIF decoder so iPhone defaults work
try:
    import pillow_heif  # type: ignore[import]
    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover — install enforced via pyproject
    pass

log = structlog.get_logger()

TARGET_W = 1080
TARGET_H = 1920
FPS = 30
MAX_INPUT_BYTES = 25 * 1024 * 1024  # 25MB cap for direct upload


class ImageConversionError(Exception):
    """Raised when an image cannot be processed into a clip."""


def _normalize_to_9x16(raw: bytes) -> bytes:
    """Decode, EXIF-correct, center-crop/pad to 1080x1920, return PNG bytes.

    PIL decodes pixel data lazily — Image.open() only reads the header. The
    actual decode happens later (in exif_transpose, resize, save), which is
    where truncated or corrupt streams raise OSError. Wrap the whole pipeline
    so any decode/encode failure surfaces as a clean ImageConversionError
    (caught by the route as 422), not an uncaught 500.
    """
    try:
        img = Image.open(io.BytesIO(raw))

        # Apply EXIF orientation (iPhones store rotation as a tag, not pixels)
        img = ImageOps.exif_transpose(img)

        # Convert to RGB (drops alpha; FFmpeg yuv420p needs no alpha anyway)
        if img.mode != "RGB":
            img = img.convert("RGB")

        # Cover-fit to 9:16: scale so the shorter axis fills, then center-crop
        src_w, src_h = img.size
        target_ratio = TARGET_W / TARGET_H  # 0.5625
        src_ratio = src_w / src_h
        if src_ratio > target_ratio:
            # Source wider than 9:16 — scale by height, crop sides
            new_h = TARGET_H
            new_w = round(src_w * (TARGET_H / src_h))
        else:
            # Source taller (or equal) than 9:16 — scale by width, crop top/bottom
            new_w = TARGET_W
            new_h = round(src_h * (TARGET_W / src_w))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # Center crop
        left = (new_w - TARGET_W) // 2
        top = (new_h - TARGET_H) // 2
        img = img.crop((left, top, left + TARGET_W, top + TARGET_H))

        out = io.BytesIO()
        img.save(out, format="PNG", optimize=False)
        return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageConversionError(f"Could not decode image: {exc}") from exc


def image_bytes_to_mp4(
    raw: bytes,
    out_path: str,
    duration_s: float,
) -> None:
    """Convert raw image bytes into a static mp4 of `duration_s` seconds at out_path.

    Output: 1080x1920, 30fps, H.264 yuv420p, no audio.
    """
    if len(raw) > MAX_INPUT_BYTES:
        raise ImageConversionError(
            f"Image exceeds {MAX_INPUT_BYTES // (1024 * 1024)}MB limit "
            f"({len(raw)} bytes)"
        )

    duration_s = max(0.5, float(duration_s))
    png_bytes = _normalize_to_9x16(raw)

    with tempfile.TemporaryDirectory(prefix="nova_img2vid_") as tmp:
        png_path = Path(tmp) / "src.png"
        png_path.write_bytes(png_bytes)

        cmd = [
            "ffmpeg", "-y",
            # -loglevel error + -nostats: drop the per-frame progress stream
            # from stderr so that `proc.stderr` on a non-zero exit contains
            # the actual error line(s), not `frame=N fps=… size=…` noise.
            # Without this, the tail-slice we surface to the user was always
            # progress and the real error was truncated off the front.
            "-loglevel", "error",
            "-nostats",
            "-loop", "1",
            "-framerate", str(FPS),
            "-i", str(png_path),
            "-t", f"{duration_s:.3f}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "veryfast",
            "-crf", "20",
            # Tag explicit bt709 color metadata so the downstream reframe
            # pipeline's colorspace handling doesn't see "primaries=unknown"
            # and reject the clip. The muxer-level -color_primaries /
            # -colorspace flags below set the container hints, but libx264
            # does not propagate them into the H.264 SPS without an explicit
            # -x264-params override. Without this, ffprobe reads back
            # primaries=unknown,matrix=unknown and the reframe colorspace
            # filter fails with EINVAL (rc=234 in prod, job 2795fa69).
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
            "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
            "-movflags", "+faststart",
            out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            log.error(
                "ffmpeg_image_to_mp4_failed",
                returncode=proc.returncode,
                stderr=stderr,
                stdout=proc.stdout.strip(),
                out_path=out_path,
                duration_s=duration_s,
                png_bytes=len(png_bytes),
            )
            tail = stderr.splitlines()[-1] if stderr else f"ffmpeg exited {proc.returncode}"
            raise ImageConversionError(f"Could not encode image: {tail}")

    log.info(
        "image_to_mp4_done",
        out_path=out_path,
        duration_s=duration_s,
        out_size_bytes=Path(out_path).stat().st_size,
    )
