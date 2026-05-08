"""Render a still image as a 1080x1920 mp4 clip, optionally with Ken Burns zoom.

Used by the music orchestrator when:
  1. A recipe slot is `slot_type="fixed_asset"` with `asset_kind="image"` (template-defined still)
  2. A user uploads a JPEG/PNG instead of a video for a `user_upload` slot

Output spec matches reframe.py's final clip spec so the mp4 plugs straight into
_assemble_clips' phase-2 join (no transcode mismatch at concat time).

Ken Burns implementation:
  Uses FFmpeg's `zoompan` filter on a high-resolution upscaled source. zoompan
  defaults to a 25fps timebase regardless of the input image, so we feed it via
  `-r {fps}` and pass `d=fps*duration` frames to keep timing exact. The image
  is first padded to a 9:16 canvas at 4x output resolution (4320x7680) so the
  pan does not magnify aliasing.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass

import structlog

from app.config import settings

log = structlog.get_logger()


class ImageClipError(Exception):
    pass


@dataclass
class KenBurns:
    """Ken Burns parameters. zoom_to=1.0 means no zoom."""

    zoom_from: float = 1.0
    zoom_to: float = 1.08  # subtle 8% zoom-in over the slot
    pan_x: str = "iw/2-(iw/zoom/2)"  # centered horizontally
    pan_y: str = "ih/2-(ih/zoom/2)"  # centered vertically


# Subtle default — matches "hafif zoom-in" feel
SUBTLE_ZOOM_IN = KenBurns(zoom_from=1.0, zoom_to=1.08)
NO_MOTION = KenBurns(zoom_from=1.0, zoom_to=1.0)

_PRESCALE_W = 4320
_PRESCALE_H = 7680


def render_image_to_clip(
    image_path: str,
    output_path: str,
    duration_s: float,
    ken_burns: KenBurns | None = None,
    fps: int | None = None,
) -> None:
    """Render a still image as a duration_s mp4 at output spec.

    Raises ImageClipError if FFmpeg returns non-zero or output is empty.
    """
    if duration_s <= 0:
        raise ImageClipError(f"duration_s must be > 0, got {duration_s}")
    if not os.path.exists(image_path):
        raise ImageClipError(f"image not found: {image_path}")

    target_fps = fps or settings.output_fps
    motion = ken_burns or NO_MOTION
    width = settings.output_width
    height = settings.output_height
    total_frames = max(1, int(round(duration_s * target_fps)))

    # Pre-scale + pad image to 9:16 canvas at 4x resolution before zoompan,
    # so the zoom doesn't reveal pixel-level aliasing on the user image.
    # Force-scale to fit (preserve aspect, pad with black) — input may be 1:1
    # (TikTok thumbnail) or 16:9.
    pad_filter = (
        f"scale={_PRESCALE_W}:{_PRESCALE_H}:force_original_aspect_ratio=decrease,"
        f"pad={_PRESCALE_W}:{_PRESCALE_H}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1"
    )

    if motion.zoom_from == motion.zoom_to == 1.0:
        # Static hold — skip zoompan entirely (cheaper, no rasterization artifacts)
        vf = f"{pad_filter},scale={width}:{height},setsar=1,fps={target_fps}"
    else:
        # Linear zoom from zoom_from → zoom_to over total_frames.
        # zoompan expression `on` is the current output frame index (0-based);
        # we map [0..total_frames-1] linearly to [zoom_from..zoom_to].
        zoom_expr = (
            f"{motion.zoom_from}+({motion.zoom_to}-{motion.zoom_from})"
            f"*on/{max(1, total_frames - 1)}"
        )
        vf = (
            f"{pad_filter},"
            f"zoompan=z='{zoom_expr}':x='{motion.pan_x}':y='{motion.pan_y}'"
            f":d={total_frames}:fps={target_fps}:s={width}x{height},"
            f"setsar=1"
        )

    cmd = [
        "ffmpeg",
        "-loop", "1",
        "-r", str(target_fps),
        "-i", image_path,
        "-t", f"{duration_s:.3f}",
        "-vf", vf,
        "-c:v", "libx264",
        "-profile:v", "high",
        "-preset", "ultrafast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-b:v", settings.output_video_bitrate,
        "-maxrate", settings.output_video_bitrate,
        "-bufsize", "8M",
        "-r", str(target_fps),
        "-an",
        "-movflags", "+faststart",
        "-y",
        output_path,
    ]

    log.info(
        "image_clip_render_start",
        image=os.path.basename(image_path),
        duration_s=round(duration_s, 3),
        zoom=f"{motion.zoom_from:.2f}->{motion.zoom_to:.2f}",
        frames=total_frames,
    )

    result = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
    if result.returncode != 0:
        stderr_tail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise ImageClipError(
            f"FFmpeg image→clip failed (rc={result.returncode}): {stderr_tail}"
        )
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise ImageClipError(f"FFmpeg produced empty output: {output_path}")

    log.info("image_clip_render_done", output=os.path.basename(output_path))


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


def is_image_file(path: str) -> bool:
    """Return True when *path* looks like a still image by extension."""
    ext = os.path.splitext(path)[1].lower()
    return ext in _IMAGE_EXTS


def render_video_to_clip(
    input_path: str,
    output_path: str,
    start_s: float,
    duration_s: float,
    aspect_ratio: str = "9:16",
) -> None:
    """Reframe a user-uploaded video to 1080x1920 at the output spec.

    Sibling of `render_image_to_clip`, used by the templated music
    orchestrator. Sidesteps `reframe.py`'s `colorspace=all=bt709` filter
    (which rejects "unknown" primaries / HLG transfer characteristics seen on
    iPhone HEVC and some compressed h264 web exports) by pinning the input
    parameters with `setparams` before the scale/pad chain. The output is
    tagged bt709 SDR.

    Args:
        input_path: any video that FFmpeg can decode.
        output_path: target mp4.
        start_s: inclusive start in seconds.
        duration_s: clip length.
        aspect_ratio: "9:16" (scale + pad) or "16:9" (scale + center crop).
    """
    if duration_s <= 0:
        raise ImageClipError(f"duration_s must be > 0, got {duration_s}")
    if not os.path.exists(input_path):
        raise ImageClipError(f"video not found: {input_path}")

    width = settings.output_width
    height = settings.output_height
    fps = settings.output_fps

    # Force "unknown"/exotic input metadata to bt709 SDR. This is what
    # reframe.py's colorspace filter cannot do for arib-std-b67 or
    # unknown-primaries clips.
    setparams = (
        "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709"
    )

    if aspect_ratio == "16:9":
        scale_pad = (
            f"scale=-2:{height},"
            f"crop={width}:{height}"
        )
    else:
        # 9:16 input or unknown — fit into the canvas, pad with black.
        scale_pad = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )

    vf = f"{setparams},{scale_pad},setsar=1,fps={fps},format=yuv420p"

    cmd = [
        "ffmpeg",
        "-ss", f"{start_s:.3f}",
        "-t", f"{duration_s:.3f}",
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-profile:v", "high",
        "-preset", "ultrafast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-b:v", settings.output_video_bitrate,
        "-maxrate", settings.output_video_bitrate,
        "-bufsize", "8M",
        "-r", str(fps),
        "-an",  # audio is mixed in later from the music track
        "-movflags", "+faststart",
        "-y",
        output_path,
    ]

    log.info(
        "video_clip_render_start",
        input=os.path.basename(input_path),
        start_s=round(start_s, 3),
        duration_s=round(duration_s, 3),
        aspect=aspect_ratio,
    )

    result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        stderr_tail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise ImageClipError(
            f"FFmpeg video→clip failed (rc={result.returncode}): {stderr_tail}"
        )
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise ImageClipError(f"FFmpeg produced empty output: {output_path}")

    log.info("video_clip_render_done", output=os.path.basename(output_path))


def normalize_to_jpeg(input_path: str, output_path: str) -> None:
    """Convert any image format to baseline JPEG (handles HEIC/WEBP→JPEG via FFmpeg).

    Some user uploads are HEIC (iPhone) which FFmpeg's libavformat handles via
    libheif when present. If conversion fails, callers should fall back to
    treating the original file as the source.
    """
    if input_path.lower().endswith((".jpg", ".jpeg")):
        shutil.copy2(input_path, output_path)
        return
    cmd = [
        "ffmpeg",
        "-i", input_path,
        "-q:v", "2",
        "-y",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    if result.returncode != 0:
        stderr_tail = result.stderr.decode("utf-8", errors="replace")[-500:]
        raise ImageClipError(
            f"FFmpeg image normalize failed (rc={result.returncode}): {stderr_tail}"
        )
