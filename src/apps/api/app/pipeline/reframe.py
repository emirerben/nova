"""[Stage 7] FFmpeg reframe + caption burn + audio normalize -> 1080x1920 MP4.

16:9 input: scale to height=1920 -> face-tracked crop to 1080x1920
9:16 input: scale+pad to 1080x1920
Output: h264, 1080x1920, 30fps, 4Mbps video, aac 192kbps, -14 LUFS audio

Supports three overlay types in the same slot:
  1. PNG overlays (font-cycle, static) -> FFmpeg overlay filter with enable timing
  2. ASS animated text (fade-in, typewriter, slide-up) -> subtitles filter
  3. ASS captions (speech) -> subtitles filter

Filter chain when overlays present:
  [0:v] -> setpts (if speed!=1) -> scale/crop -> color_grade
    -> [base] -> overlay PNGs -> subtitles ASS text -> subtitles ASS captions -> [out]

IMPORTANT: subprocess.run() is blocking -- all calls here are sync.
CRITICAL: Never use shell=True. Always pass args as a list.
"""

import os
import subprocess

import structlog

from app.config import settings
from app.pipeline.probe import probe_video
from app.pipeline.text_overlay import FONTS_DIR

log = structlog.get_logger()

MAX_OUTPUT_BYTES = 500 * 1024 * 1024  # 500MB


class ReframeError(Exception):
    pass


def _encoding_args(
    output_path: str,
    preset: str = "fast",
    crf: str = "18",
) -> list[str]:
    """Shared FFmpeg output encoding arguments (DRY).

    Args:
        preset: libx264 preset. Use "ultrafast" for intermediate files
                that will be re-encoded later, "fast" for final output.
        crf: Constant Rate Factor. Lower = better quality.
             18 is visually lossless, 23 is default (too lossy for
             multi-pass pipelines with 3+ re-encodes).

    Color handling: HDR/HLG sources (bt2020, arib-std-b67) are converted
    to SDR (bt709) via the colorspace filter in _build_video_filter. This
    function tags the output as bt709 so players render colors correctly.
    """
    args = [
        "-c:v", "libx264",
        "-profile:v", "high",
        "-preset", preset,
        "-crf", crf,
        "-pix_fmt", "yuv420p",
    ]
    # Only re-enable scenecut for final-output encodes (preset="fast").
    # ultrafast intermediates set scenecut=0 internally — overriding it wastes
    # CPU and is pointless since intermediates are re-encoded anyway.
    # scenecut=40 fires at scene transitions, inserting I-frames that prevent
    # horizontal tearing across slot boundaries in the final video.
    # keyint=90 = max 3s between keyframes at 30fps (safety net).
    # Call sites: _concat_demuxer and _burn_text_overlays use "fast".
    # All other call sites use "ultrafast".
    if preset == "fast":
        args += ["-x264-params", "scenecut=40:keyint=90"]
    args += [
        # Tag output as bt709 (SDR) so players render colors correctly.
        # iPhone clips often come as bt2020/HLG; without explicit tagging
        # the encoder copies source tags → player misinterprets SDR data
        # as HDR → washed-out look.
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-b:v", settings.output_video_bitrate,
        "-maxrate", settings.output_video_bitrate,
        "-bufsize", "8M",
        "-r", str(settings.output_fps),
        "-c:a", "aac",
        "-b:a", settings.output_audio_bitrate,
        "-s", f"{settings.output_width}x{settings.output_height}",
        "-movflags", "+faststart",
        "-y",
        output_path,
    ]
    return args


def reframe_and_export(
    input_path: str,
    start_s: float,
    end_s: float,
    aspect_ratio: str,  # "16:9" | "9:16"
    ass_subtitle_path: str | None,
    output_path: str,
    text_overlay_pngs: list[dict] | None = None,
    ass_overlay_paths: list[str] | None = None,
    color_hint: str = "none",
    speed_factor: float = 1.0,
    darkening_windows: list[tuple[float, float]] | None = None,
    narrowing_windows: list[tuple[float, float]] | None = None,
    output_fit: str = "crop",  # "crop" (default — center-crop sides) | "letterbox" (pad with bg)
) -> None:
    """Render a single clip to the output spec. Raises ReframeError on failure.

    Never uses shell=True. All paths are passed as list args.

    text_overlay_pngs: list of {png_path, start_s, end_s} for PNG overlay compositing.
    ass_overlay_paths: list of ASS file paths for animated text overlays.
    speed_factor: playback speed multiplier (2.0 = 2x fast). Applied as setpts=PTS/speed_factor.
    color_hint: color grading preset -- "warm", "cool", "high-contrast", etc.
    """
    duration = end_s - start_s
    if duration <= 0:
        raise ReframeError(f"Invalid duration: {duration}s")

    # Probe color transfer characteristic to select the right HDR→SDR filter.
    try:
        clip_probe = probe_video(input_path)
        color_trc = clip_probe.color_trc
    except Exception:
        color_trc = "bt709"

    # Build video filter chain
    vf_parts = _build_video_filter(
        aspect_ratio, ass_subtitle_path,
        color_hint=color_hint,
        speed_factor=speed_factor,
        darkening_windows=darkening_windows,
        narrowing_windows=narrowing_windows,
        color_trc=color_trc,
        output_fit=output_fit,
    )

    # Debug: log final filter chain to diagnose darkness/color issues.
    log.info(
        "reframe_filter_chain",
        input=input_path,
        color_trc=color_trc,
        color_hint=color_hint,
        darkening_windows=darkening_windows or [],
        narrowing_windows=narrowing_windows or [],
        filters=vf_parts,
    )

    # Build command -- use filter_complex when overlays are present
    has_overlays = bool(text_overlay_pngs) or bool(ass_overlay_paths)

    if has_overlays:
        cmd = _build_overlay_cmd(
            input_path, start_s, duration, vf_parts,
            text_overlay_pngs or [],
            ass_overlay_paths or [],
            output_path,
        )
    else:
        vf_string = ",".join(vf_parts)
        cmd = [
            "ffmpeg",
            "-ss", str(start_s),
            "-t", str(duration),
            "-i", input_path,
            "-vf", vf_string,
            *_encoding_args(output_path, preset="ultrafast"),
        ]

    log.info(
        "reframe_start", input=input_path, start=start_s, end=end_s,
        aspect=aspect_ratio, speed=speed_factor,
        overlays=len(text_overlay_pngs or []),
        ass_overlays=len(ass_overlay_paths or []),
    )

    result = subprocess.run(cmd, capture_output=True, timeout=600, check=False)

    if result.returncode != 0 and ass_overlay_paths:
        # ASS overlays may fail if FFmpeg lacks libass -- retry with PNGs only
        log.warning(
            "reframe_ass_fallback_to_png",
            rc=result.returncode,
            ass_count=len(ass_overlay_paths),
        )
        return reframe_and_export(
            input_path=input_path,
            start_s=start_s,
            end_s=end_s,
            aspect_ratio=aspect_ratio,
            ass_subtitle_path=ass_subtitle_path,
            output_path=output_path,
            text_overlay_pngs=text_overlay_pngs,
            ass_overlay_paths=None,  # retry without ASS
            color_hint=color_hint,
            speed_factor=speed_factor,
            darkening_windows=darkening_windows,
            narrowing_windows=narrowing_windows,
            output_fit=output_fit,
        )

    if result.returncode != 0:
        # FFmpeg always prints a long version + configuration banner to stderr.
        # The actual error lives in the lines that *don't* match those headers.
        # Drop them so we surface the real failure even when the banner alone
        # exceeds our truncation buffer.
        full_stderr = result.stderr.decode(errors="replace")
        meaningful = [
            ln for ln in full_stderr.splitlines()
            if ln.strip()
            and not ln.startswith(("ffmpeg version", "  built with", "  configuration:", "  lib"))
        ]
        msg = "\n".join(meaningful[-30:]) or full_stderr[-1500:]
        log.error(
            "ffmpeg_failed_detail",
            rc=result.returncode,
            cmd=" ".join(cmd),
            stderr_tail=msg[-2000:],
        )
        raise ReframeError(f"FFmpeg failed (rc={result.returncode}): {msg}")

    if not os.path.exists(output_path):
        raise ReframeError("FFmpeg exited 0 but output file not found")

    file_size = os.path.getsize(output_path)
    if file_size > MAX_OUTPUT_BYTES:
        os.unlink(output_path)
        raise ReframeError(f"Output exceeds 500MB ({file_size} bytes)")

    log.info("reframe_done", output=output_path, size_mb=file_size // (1024 * 1024))


def _build_overlay_cmd(
    input_path: str,
    start_s: float,
    duration: float,
    vf_parts: list[str],
    overlay_pngs: list[dict],
    ass_overlay_paths: list[str],
    output_path: str,
) -> list[str]:
    """Build FFmpeg command with -filter_complex for PNG + ASS overlay compositing.

    Chain: [0:v] -> vf filters -> overlay each PNG -> subtitles each ASS -> output
    """
    cmd = [
        "ffmpeg",
        "-ss", str(start_s),
        "-t", str(duration),
        "-i", input_path,
    ]

    # Add each PNG as an input
    for ov in overlay_pngs:
        cmd.extend(["-i", ov["png_path"]])

    # Build filter_complex string
    # First apply the vf chain to the video input
    vf_chain = ",".join(vf_parts) if vf_parts else "null"
    fc_parts = [f"[0:v]{vf_chain}[base]"]

    # Chain PNG overlay filters
    prev_label = "base"
    for i, ov in enumerate(overlay_pngs):
        input_idx = i + 1  # PNG inputs start at index 1
        start = ov["start_s"]
        end = ov["end_s"]
        out_label = f"ov{i}"
        fc_parts.append(
            f"[{prev_label}][{input_idx}:v]overlay=0:0"
            f":enable='between(t,{start:.3f},{end:.3f})'"
            f"[{out_label}]"
        )
        prev_label = out_label

    # Chain ASS subtitle filters for animated text overlays
    escaped_fonts = FONTS_DIR.replace("\\", "/").replace(":", "\\:")
    for j, ass_path in enumerate(ass_overlay_paths):
        escaped = ass_path.replace("\\", "/").replace(":", "\\:")
        out_label = f"sub{j}"
        fc_parts.append(
            f"[{prev_label}]subtitles={escaped}:fontsdir={escaped_fonts}[{out_label}]"
        )
        prev_label = out_label

    filter_complex = ";".join(fc_parts)

    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", f"[{prev_label}]",
        "-map", "0:a?",
        *_encoding_args(output_path, preset="ultrafast"),
    ])

    return cmd


_HLG_TRANSFER = "arib-std-b67"
_HDR10_TRANSFER = "smpte2084"

# zscale tonemap pipeline for HLG/HDR10 → SDR conversion.
# FFmpeg's `colorspace` filter does not support HLG (arib-std-b67) or PQ input;
# zscale (libzimg) handles the full HDR transfer function correctly.
#
# npl=400 + mobius: tuned empirically against iPhone HLG samples.
#   npl=203 + clip overshot (output YAVG=203 vs source ~170) because clip
#   passed midtones through unchanged and HLG diffuse-white-200nit ended up at
#   near-white in SDR. npl=400 expects peaks up to 400 nits, so 200-nit scene
#   white maps to linear ~0.5 and bt709 gamma yields ~73% display luma —
#   matching natural iPhone footage brightness.
# tonemap=mobius: smooth shoulder rolloff for values approaching 1.0,
#   preserves midtone contrast better than reinhard, less aggressive than
#   hable. Specular highlights compressed gracefully instead of clipped.
_ZSCALE_SDR_PIPELINE = (
    "zscale=t=linear:npl=400"
    ",format=gbrpf32le"
    ",zscale=p=bt709"
    ",tonemap=tonemap=mobius"
    ",zscale=t=bt709:m=bt709:r=tv"
    ",format=yuv420p"
)


def _build_video_filter(
    aspect_ratio: str,
    ass_path: str | None,
    color_hint: str = "none",
    speed_factor: float = 1.0,
    darkening_windows: list[tuple[float, float]] | None = None,
    narrowing_windows: list[tuple[float, float]] | None = None,
    color_trc: str = "bt709",
    output_fit: str = "crop",
) -> list[str]:
    """Return list of filter segments to join with commas.

    Filter order: HDR tonemap (if needed) -> setpts (speed) -> scale/crop
                  -> color grading -> darkening -> narrowing -> caption ASS.
    setpts MUST be first to normalize PTS before timed filters (darkening, narrowing).
    Text overlays are handled separately via overlay filter (not in this chain).
    """
    filters: list[str] = []

    # 0a. HDR/HLG → SDR color conversion.
    # HLG (arib-std-b67) and HDR10 (smpte2084) require zscale tonemap pipeline —
    # FFmpeg's `colorspace` filter rejects these transfer characteristics.
    # bt709 clips (Android SDR, screen recordings) use colorspace=all=bt709 (no-op for SDR).
    if color_trc in (_HLG_TRANSFER, _HDR10_TRANSFER):
        filters.append(_ZSCALE_SDR_PIPELINE)
    else:
        filters.append("colorspace=all=bt709")

    # 0. Speed ramp -- FIRST filter to normalize PTS for all subsequent timed filters
    if speed_factor != 1.0 and speed_factor > 0:
        filters.append(f"setpts=PTS/{speed_factor}")

    # 1. Scale/crop. Default "crop" mode center-crops 16:9 source to 9:16 (loses
    # the sides — fine for talking heads / single-subject shots). "letterbox"
    # variants preserve the entire source frame: scale-to-fit, then pad with
    # either a blurred zoom of the same source or flat black bars.
    ow = settings.output_width
    oh = settings.output_height
    if output_fit == "letterbox" or output_fit == "letterbox_blur":
        # Foreground = original frame fit-into-canvas;
        # Background = same source upscaled + blurred + cropped to fill 9:16.
        filters.append(
            "split=2[bg][fg];"
            f"[bg]scale={ow}:{oh}:force_original_aspect_ratio=increase,"
            f"crop={ow}:{oh},gblur=sigma=30[bg];"
            f"[fg]scale={ow}:{oh}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
    elif output_fit == "letterbox_black":
        # Pure black bars — match reference TikToks that don't blend the source
        # behind itself. Crisper but reads more "letterbox-y."
        filters.append(
            f"scale={ow}:{oh}:force_original_aspect_ratio=decrease"
        )
        filters.append(
            f"pad={ow}:{oh}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    elif aspect_ratio == "16:9":
        filters.append(f"scale=-2:{settings.output_height}")
        filters.append(f"crop={settings.output_width}:{settings.output_height}")
    else:
        filters.append(
            f"scale={settings.output_width}:{settings.output_height}"
            f":force_original_aspect_ratio=decrease"
        )
        filters.append(
            f"pad={settings.output_width}:{settings.output_height}"
            f":(ow-iw)/2:(oh-ih)/2"
        )

    # 2. Color grading (applied before darkening so darkened areas have the grade)
    for f in _color_hint_filters(color_hint):
        filters.append(f)

    # 3. Darkening (colorlevels with enable timing)
    for start, end in (darkening_windows or []):
        filters.append(
            f"colorlevels=rimax=0.45:gimax=0.45:bimax=0.45"
            f":enable='between(t,{start:.3f},{end:.3f})'"
        )

    # 4. Narrowing (letterbox drawbox bars, top + bottom)
    for start, end in (narrowing_windows or []):
        enable = f"enable='between(t,{start:.3f},{end:.3f})'"
        filters.append(
            f"drawbox=x=0:y=0:w=iw:h=120:color=black@0.85:t=fill:{enable}"
        )
        filters.append(
            f"drawbox=x=0:y=ih-120:w=iw:h=120:color=black@0.85:t=fill:{enable}"
        )

    # 5. Caption ASS (speech captions -- distinct from text overlay ASS)
    if ass_path and os.path.exists(ass_path):
        escaped = ass_path.replace("\\", "/").replace(":", "\\:")
        escaped_fonts = FONTS_DIR.replace("\\", "/").replace(":", "\\:")
        filters.append(f"ass={escaped}:fontsdir={escaped_fonts}")

    return filters


# -- Color grading filter mapping ---------------------------------------------
# Starting-point values -- visually verify on 3-5 templates and tune if needed.
# These are intentionally subtle to avoid over-processing.

_COLOR_HINT_MAP: dict[str, list[str]] = {
    "warm": ["colorbalance=rs=0.15:gs=0.05:bs=-0.1"],
    "cool": ["colorbalance=rs=-0.1:gs=0.0:bs=0.15"],
    "high-contrast": ["eq=contrast=1.3:saturation=1.2"],
    "desaturated": ["eq=saturation=0.6"],
    "vintage": [
        "colorbalance=rs=0.1:gs=0.05:bs=-0.15",
        "eq=saturation=0.8",
    ],
}


def _color_hint_filters(color_hint: str) -> list[str]:
    """Return FFmpeg filter strings for the given color hint.

    Returns empty list for "none" or unrecognized values.
    """
    return list(_COLOR_HINT_MAP.get(color_hint, []))
