"""FFmpeg-backed slide normalization, preview stitching, and export-bundle
assembly for mixed-media "slide posts" (ordered images + videos).

Pure filesystem functions — no GCS, no DB. The caller (the dispatch task in
`app/tasks/generative_build.py`) owns download/upload; every function here
takes and returns local paths. Mirrors the separation already established by
`app/pipeline/image_clip.py`.

Encoder policy: the normalized per-slide derivatives and the stitched preview
are the bytes that ship in the export bundle and the hero preview — FINAL
output, `preset="fast"` or stricter (see CLAUDE.md "Encoder policy"). This
module does NOT call the shared `app.pipeline.reframe._encoding_args` helper
— that helper is coupled to the main reframe pipeline's HDR/canvas handling,
which a slide post (a still-image-heavy carousel, not a continuous shot) does
not need. Same quality policy, a simpler direct implementation; the guard at
`tests/test_encoder_policy.py` audits `_encoding_args` call sites specifically
and does not need to know about this module.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

log = structlog.get_logger()

Canvas = tuple[int, int]

# Default per-image hold in the stitched PREVIEW (the export bundle's own
# image files are full-resolution stills with no duration concept — this
# only paces the scrubbable preview MP4).
DEFAULT_IMAGE_HOLD_S = 2.5
# A video slide's full length always ships in the export bundle; the preview
# only plays a capped slice of it so the preview stays short regardless of
# how many/how long the video slides are.
MAX_PREVIEW_VIDEO_SLICE_S = 4.0

_FINAL_PRESET = "fast"
_FINAL_CRF = "20"


class SlideBuildError(Exception):
    """Raised when FFmpeg fails to normalize, stitch, or extract a slide."""


def _run_ffmpeg(cmd: list[str], *, context: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        non_empty = [ln for ln in stderr.splitlines() if ln.strip()]
        tail = " | ".join(non_empty[-3:]) if non_empty else f"ffmpeg exited {proc.returncode}"
        log.error(
            "slide_post_ffmpeg_failed",
            context=context,
            returncode=proc.returncode,
            stderr=stderr,
        )
        raise SlideBuildError(f"{context}: {tail}")


def probe_dimensions(path: str) -> tuple[int, int]:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=,",
            path,
        ],
        text=True,
    ).strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def probe_duration_s(path: str) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        text=True,
    ).strip()
    return float(out)


def _scale_crop_filter(canvas: Canvas) -> str:
    cw, ch = canvas
    # Cover-fit: scale so the shorter axis fills the target, then center-crop
    # the overflow. Same fit policy as image_to_video._normalize_to_9x16,
    # expressed as one ffmpeg filter instead of a PIL resize+crop pass.
    return f"scale={cw}:{ch}:force_original_aspect_ratio=increase,crop={cw}:{ch}"


def normalize_image_slide(src_path: str, out_path: str, *, canvas: Canvas) -> None:
    """Normalize any image format (incl. HEIC/WebP) to a cover-fit JPEG."""
    if not Path(src_path).exists():
        raise SlideBuildError(f"image slide not found: {src_path}")
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "warning",
        "-nostats",
        "-i",
        src_path,
        "-vf",
        _scale_crop_filter(canvas),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        out_path,
    ]
    _run_ffmpeg(cmd, context="normalize_image_slide")


def normalize_video_slide(src_path: str, out_path: str, *, canvas: Canvas) -> None:
    """Normalize a video slide to the platform canvas, keeping its own audio.

    This is the derivative that ships in the export bundle at full length —
    unlike the preview segment below, it is never trimmed.
    """
    if not Path(src_path).exists():
        raise SlideBuildError(f"video slide not found: {src_path}")
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "warning",
        "-nostats",
        "-i",
        src_path,
        "-vf",
        f"{_scale_crop_filter(canvas)},fps=30",
        "-c:v",
        "libx264",
        "-preset",
        _FINAL_PRESET,
        "-crf",
        _FINAL_CRF,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        out_path,
    ]
    _run_ffmpeg(cmd, context="normalize_video_slide")


def render_preview_segment(
    normalized_path: str,
    out_path: str,
    *,
    kind: Literal["image", "video"],
    canvas: Canvas,
    hold_s: float = DEFAULT_IMAGE_HOLD_S,
) -> None:
    """Render one slide's silent, capped-duration segment for the stitched preview."""
    cw, ch = canvas
    # setsar=1 is required, not cosmetic: the image and video encode paths
    # can each independently pick up a non-square Sample Aspect Ratio (a PNG
    # decoder's default vs. an already-1:1 H.264 stream), and the concat
    # FILTER (unlike the demuxer) hard-rejects joining segments whose SAR
    # differs, even when the pixel dimensions already match exactly.
    if kind == "image":
        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "warning",
            "-nostats",
            "-loop",
            "1",
            "-framerate",
            "30",
            "-i",
            normalized_path,
            "-t",
            f"{max(0.5, hold_s):.3f}",
            "-vf",
            f"scale={cw}:{ch},setsar=1",
            "-c:v",
            "libx264",
            "-preset",
            _FINAL_PRESET,
            "-crf",
            _FINAL_CRF,
            "-pix_fmt",
            "yuv420p",
            out_path,
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "warning",
            "-nostats",
            "-i",
            normalized_path,
            "-t",
            f"{max(0.5, hold_s):.3f}",
            "-an",
            "-vf",
            f"scale={cw}:{ch},setsar=1",
            "-c:v",
            "libx264",
            "-preset",
            _FINAL_PRESET,
            "-crf",
            _FINAL_CRF,
            "-pix_fmt",
            "yuv420p",
            out_path,
        ]
    _run_ffmpeg(cmd, context="render_preview_segment")


def concat_preview_segments(segment_paths: list[str], out_path: str, *, canvas: Canvas) -> None:
    """Concatenate pre-rendered (silent, same-canvas) segments into one preview mp4.

    Uses the concat FILTER (re-encoding), not the concat demuxer, so
    independently-encoded segments always join on a clean keyframe boundary
    regardless of how each was produced.
    """
    if not segment_paths:
        raise SlideBuildError("concat_preview_segments requires at least one segment")
    cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-nostats"]
    for path in segment_paths:
        cmd += ["-i", path]
    n = len(segment_paths)
    filter_inputs = "".join(f"[{i}:v]" for i in range(n))
    filter_complex = f"{filter_inputs}concat=n={n}:v=1:a=0[outv]"
    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[outv]",
        "-c:v",
        "libx264",
        "-preset",
        _FINAL_PRESET,
        "-crf",
        _FINAL_CRF,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        out_path,
    ]
    _run_ffmpeg(cmd, context="concat_preview_segments")


def extract_cover(
    normalized_slide_path: str, kind: Literal["image", "video"], out_path: str
) -> None:
    """Produce the cover JPEG for a slide already normalized to the canvas."""
    if kind == "image":
        shutil.copy2(normalized_slide_path, out_path)
        return
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "warning",
        "-nostats",
        "-i",
        normalized_slide_path,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        out_path,
    ]
    _run_ffmpeg(cmd, context="extract_cover")


@dataclass(frozen=True)
class BundleSlideFile:
    local_path: str
    kind: Literal["image", "video"]
    index: int
    alt: str | None = None

    @property
    def arcname(self) -> str:
        ext = "jpg" if self.kind == "image" else "mp4"
        return f"slides/{self.index:02d}.{ext}"


def build_post_manifest(
    *,
    platform_profile: str,
    caption: str,
    cover_index: int,
    slides: list[BundleSlideFile],
) -> dict:
    """The `post.json` manifest — the platform-agnostic, order-authoritative
    description of the export bundle's contents."""
    return {
        "schema_version": 1,
        "platform_profile": platform_profile,
        "caption": caption,
        "cover_index": cover_index,
        "slides": [
            {
                "index": s.index,
                "kind": s.kind,
                "filename": s.arcname.removeprefix("slides/"),
                "alt": s.alt,
            }
            for s in slides
        ],
    }


def build_bundle_zip(
    out_zip_path: str, *, slides: list[BundleSlideFile], manifest: dict, caption: str
) -> None:
    """Assemble the downloadable `bundle.zip`: ordered slide files + post.json + caption.txt."""
    with zipfile.ZipFile(out_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for slide in slides:
            zf.write(slide.local_path, arcname=slide.arcname)
        zf.writestr("post.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        zf.writestr("caption.txt", caption or "")
