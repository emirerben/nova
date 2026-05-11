"""Interstitial clip rendering and detection for compound template transitions.

Generates short video clips (curtain-close, black-hold, etc.) that are inserted
between template slots during assembly. These express transitions that can't be
modeled as a single FFmpeg xfade operation — e.g. a curtain close with a black hold.

Also provides programmatic detection of transition types by analyzing luminance
patterns in frames preceding black segments. This is more reliable than relying
solely on LLM visual analysis for classifying transition types.

CRITICAL: Never use shell=True. Always pass args as a list.
"""

import os
import re
import subprocess
import tempfile

import structlog

from app.pipeline.audio_layout import (
    BODY_SLOT_AUDIO_OUT_ARGS,
    SILENT_AUDIO_INPUT_ARGS,
)

log = structlog.get_logger()

# Number of frames to sample before a black segment for classification
_CLASSIFY_SAMPLE_COUNT = 6
# How far before the black segment start to begin sampling (seconds)
_CLASSIFY_PRE_WINDOW_S = 0.5
# Minimum ratio of middle-band to edge-band luminance to classify as curtain-close.
# A curtain close darkens edges faster than center, producing ratio > 1.5 in later frames.
_CURTAIN_CLOSE_RATIO_THRESHOLD = 1.5
# Minimum number of frames showing the bar pattern to confirm curtain-close
_CURTAIN_CLOSE_MIN_FRAMES = 2

# Minimum curtain-close animation duration — 3.0s was still too fast for dramatic effect
MIN_CURTAIN_ANIMATE_S = 4.0

# Maximum fraction of slot duration the curtain animation may consume.
# 60% leaves at least 40% visible footage before the bars begin.
_CURTAIN_MAX_RATIO = 0.6


class InterstitialError(Exception):
    pass


# Output specs matching template slot renders
_WIDTH = 1080
_HEIGHT = 1920
_FPS = 30


def render_color_hold(
    output_path: str,
    hold_s: float = 1.0,
    color: str = "black",
    width: int = _WIDTH,
    height: int = _HEIGHT,
    fps: int = _FPS,
) -> None:
    """Render a solid color hold clip.

    Used for all interstitial types: curtain-close (black hold after bars),
    fade-black-hold (black pause), flash-white (white flash). The curtain-close
    ANIMATION is applied separately via apply_curtain_close_tail() on the
    preceding slot — this function only renders the static hold portion.

    Args:
        output_path: Where to write the hold clip.
        hold_s: Duration of the hold in seconds.
        color: FFmpeg color name or hex (e.g. "black", "white", "0x000000").
        width: Output width in pixels.
        height: Output height in pixels.
        fps: Output frame rate.

    Raises:
        InterstitialError: If FFmpeg fails.
    """
    # Match body-slot layout (libx264 ultrafast yuv420p + AAC stereo 44.1k)
    # so the downstream concat can stream-copy across body slots and
    # interstitials. Audio layout constants live in audio_layout.py.
    cmd = [
        "ffmpeg",
        "-f", "lavfi",
        "-i", f"color=c={color}:s={width}x{height}:d={hold_s}:r={fps}",
        *SILENT_AUDIO_INPUT_ARGS,
        "-shortest",
        "-c:v", "libx264",
        "-profile:v", "high",
        "-preset", "ultrafast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-r", str(fps),
        "-t", f"{hold_s:.3f}",
        *BODY_SLOT_AUDIO_OUT_ARGS,
        "-movflags", "+faststart",
        "-y",
        output_path,
    ]

    log.info("interstitial_render_start", type="color_hold", hold_s=hold_s, color=color)
    result = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[:500]
        raise InterstitialError(f"color_hold render failed: {stderr}")

    log.info("interstitial_render_done", output=output_path)


def apply_curtain_close_tail(
    slot_video_path: str,
    output_path: str,
    animate_s: float = 1.0,
) -> None:
    """Apply curtain-close animation to the tail of a rendered slot clip.

    Draws two black rectangles that grow from top and bottom edges during
    the last `animate_s` seconds of the clip, meeting at the center.

    This modifies the slot's visual output — the bars overlay the actual
    footage, creating the "closing" effect before the black hold interstitial.

    Args:
        slot_video_path: Input slot .mp4 (already rendered by _render_slot).
        output_path: Where to write the modified slot.
        animate_s: Duration of the closing animation at the tail.

    Raises:
        InterstitialError: If FFmpeg fails.
    """
    # Get video duration via ffprobe
    probe_cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        slot_video_path,
    ]
    probe_result = subprocess.run(probe_cmd, capture_output=True, timeout=10, check=False)
    if probe_result.returncode != 0:
        raise InterstitialError(f"ffprobe failed: {probe_result.stderr.decode()[:200]}")

    try:
        duration = float(probe_result.stdout.decode().strip())
    except (ValueError, TypeError) as exc:
        raise InterstitialError(
            f"ffprobe returned non-numeric duration: "
            f"{probe_result.stdout.decode()[:100]}"
        ) from exc

    # Ensure visible footage before curtain starts — 60% max ratio leaves
    # at least 40% of the slot as uncovered content.
    if animate_s > duration * _CURTAIN_MAX_RATIO:
        log.warning(
            "curtain_close_clamped",
            duration=round(duration, 3),
            requested_animate_s=animate_s,
            clamped_animate_s=round(duration * _CURTAIN_MAX_RATIO, 3),
        )
        animate_s = duration * _CURTAIN_MAX_RATIO
        if animate_s < 0.05:
            raise InterstitialError(
                f"clip too short for curtain-close ({duration:.2f}s)"
            )

    anim_start = max(0.0, duration - animate_s)

    # Single-pass geq with T-conditional curtain. Earlier versions split the
    # clip into a prefix (stream-copied) and a tail (re-encoded with geq),
    # then concatenated. The concat seam produced a one-frame freeze right
    # at anim_start (visible as MAD≈0 between consecutive output frames at
    # t=anim_start+67ms). Re-encoding the prefix didn't help — the concat
    # demuxer's timestamp stitching still drops or duplicates a frame at
    # the join.
    #
    # The single-pass approach applies geq to the WHOLE clip and uses an
    # `lte(T, anim_start)` guard inside the expression so the curtain only
    # affects pixels in the tail. No split, no concat, no seam, no freeze.
    #
    # Cost: geq evaluates every pixel every frame across the full clip,
    # not just the tail. For Dimples (5.5s slot 5 at 1080x1920) the render
    # time goes from ~5s (split approach) to ~25s (full-clip geq). Worth
    # it: the freeze was visible to users and the split-and-concat seam
    # has no other fix path that survives downstream concat-demuxer.
    #
    # geq is required because drawbox's h/w/x/y expressions do NOT have
    # access to the 't' timestamp variable (only the 'enable' expression
    # does), so drawbox cannot animate bar height over time.

    log.info(
        "curtain_close_tail_start",
        input=slot_video_path,
        anim_start=round(anim_start, 3),
        animate_s=animate_s,
    )

    work_dir = tempfile.mkdtemp(prefix="curtain_")

    try:
        # Curtain progress: 0 before anim_start, then grows linearly to 1
        # over animate_s, capped at 1 thereafter. `gte(T,anim)` ensures the
        # whole prefix sees progress=0, leaving every pixel untouched.
        progress = (
            f"min(1,max(0,(T-{anim_start:.3f})/{animate_s:.3f}))"
            f"*gte(T,{anim_start:.3f})"
        )
        bar_h = f"floor(H/2*({progress}))"
        in_bar = f"lt(Y,{bar_h})+gt(Y,H-1-{bar_h})"

        geq_filter = (
            f"geq="
            f"lum='if({in_bar},0,lum(X,Y))':"
            f"cb='if({in_bar},128,cb(X,Y))':"
            f"cr='if({in_bar},128,cr(X,Y))'"
        )

        # One pass: decode → geq → re-encode video. Audio passes through
        # as a single re-encode keeping the body-slot AAC layout so the
        # downstream concat-demuxer can stream-copy this slot without
        # boundary glitches.
        single_pass_cmd = [
            "ffmpeg", "-i", slot_video_path,
            "-vf", geq_filter,
            "-c:v", "libx264", "-profile:v", "high",
            "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            *BODY_SLOT_AUDIO_OUT_ARGS,
            "-movflags", "+faststart",
            "-y", output_path,
        ]
        r = subprocess.run(single_pass_cmd, capture_output=True, timeout=180, check=False)
        if r.returncode != 0:
            raise InterstitialError(
                f"single-pass curtain-close failed: "
                f"{r.stderr.decode(errors='replace')[:500]}"
            )

    finally:
        try:
            os.rmdir(work_dir)
        except OSError:
            pass

    log.info("curtain_close_tail_done", output=output_path)


def detect_black_segments(
    video_path: str,
    black_min_duration: float = 0.15,
    pixel_threshold: float = 0.15,
    picture_threshold: float = 0.98,
) -> list[dict]:
    """Detect black segments in a video using FFmpeg blackdetect.

    Returns a list of dicts with {start_s, end_s, duration_s} for each
    detected black segment. Used during template analysis to find
    curtain-close and fade-to-black transitions programmatically.

    Args:
        video_path: Path to video file.
        black_min_duration: Minimum black segment duration to report.
        pixel_threshold: Per-pixel luminance threshold (0-1, lower = stricter).
        picture_threshold: Fraction of pixels that must be below threshold.

    Returns:
        List of black segment dicts, sorted by start time.
        Returns [] on any error (non-fatal).
    """
    cmd = [
        "ffmpeg", "-i", video_path,
        "-vf", (
            f"blackdetect=d={black_min_duration}"
            f":pix_th={pixel_threshold}"
            f":pic_th={picture_threshold}"
        ),
        "-an",
        "-f", "null", "-",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        stderr_text = result.stderr.decode(errors="replace")

        # Parse: [blackdetect @ 0x...] black_start:12.3 black_end:13.1 black_duration:0.8
        segments = []
        for m in re.finditer(
            r"black_start:\s*([\d.]+)\s*black_end:\s*([\d.]+)\s*black_duration:\s*([\d.]+)",
            stderr_text,
        ):
            segments.append({
                "start_s": float(m.group(1)),
                "end_s": float(m.group(2)),
                "duration_s": float(m.group(3)),
            })

        segments.sort(key=lambda s: s["start_s"])
        log.info("blackdetect_done", segments=len(segments), video=video_path)
        return segments

    except Exception as exc:
        log.warning("blackdetect_failed", error=str(exc), video=video_path)
        return []


# ── Transition type classification ───────────────────────────────────────────


def _sample_band_luminance(
    video_path: str,
    timestamp: float,
    band: str,
) -> float | None:
    """Compute average luminance (YAVG) for a horizontal band of a single frame.

    Args:
        video_path: Path to video file.
        timestamp: Time in seconds to sample the frame.
        band: One of "top", "middle", "bottom" — selects which third of the frame.

    Returns:
        Average luminance (0-255) for the band, or None on failure.
    """
    # crop=w:h:x:y — crop to the relevant third of the frame
    crop_y = {"top": "0", "middle": "ih/3", "bottom": "2*ih/3"}
    y_offset = crop_y.get(band, "0")

    cmd = [
        "ffmpeg",
        "-ss", f"{timestamp:.3f}",
        "-i", video_path,
        "-vf", f"crop=iw:ih/3:0:{y_offset},signalstats",
        "-frames:v", "1",
        "-f", "null", "-",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10, check=False)
        stderr_text = result.stderr.decode(errors="replace")

        # signalstats outputs: [Parsed_signalstats_1 @ ...] YAVG:123.4 ...
        m = re.search(r"YAVG:\s*([\d.]+)", stderr_text)
        if m:
            return float(m.group(1))
        return None
    except Exception:
        return None


def _sample_frame_bands(
    video_path: str,
    timestamp: float,
) -> dict[str, float] | None:
    """Sample luminance for all three horizontal bands of a frame in one FFmpeg call.

    Uses split + 3x crop + signalstats in a single filter_complex to reduce
    subprocess invocations from 3 per frame to 1.

    Returns dict with keys "top", "middle", "bottom" mapping to YAVG values,
    or None on failure.
    """
    # Single FFmpeg call: split input into 3 streams, crop each to a band,
    # run signalstats on each. Parse 3 YAVG values from stderr.
    filter_complex = (
        "split=3[s0][s1][s2];"
        "[s0]crop=iw:ih/3:0:0,signalstats[top];"
        "[s1]crop=iw:ih/3:0:ih/3,signalstats[mid];"
        "[s2]crop=iw:ih/3:0:2*ih/3,signalstats[bot]"
    )
    cmd = [
        "ffmpeg",
        "-ss", f"{timestamp:.3f}",
        "-i", video_path,
        "-filter_complex", filter_complex,
        "-map", "[top]", "-map", "[mid]", "-map", "[bot]",
        "-frames:v", "1",
        "-f", "null", "-",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10, check=False)
        stderr_text = result.stderr.decode(errors="replace")

        # Parse all YAVG values — one per signalstats instance.
        # They appear in filter order: top, middle, bottom.
        yavg_values = re.findall(r"YAVG:\s*([\d.]+)", stderr_text)
        if len(yavg_values) >= 3:
            return {
                "top": float(yavg_values[0]),
                "middle": float(yavg_values[1]),
                "bottom": float(yavg_values[2]),
            }

        # Fallback: try individual calls if filter_complex fails
        bands: dict[str, float] = {}
        for band in ("top", "middle", "bottom"):
            val = _sample_band_luminance(video_path, timestamp, band)
            if val is None:
                return None
            bands[band] = val
        return bands
    except Exception:
        return None


def classify_black_segment_type(
    video_path: str,
    black_segments: list[dict],
) -> list[dict]:
    """Classify each black segment as curtain-close, fade-black-hold, or unknown.

    Analyzes luminance patterns in the frames BEFORE each black segment to
    distinguish curtain-close (bars closing from top/bottom) from fade-to-black
    (uniform darkening).

    Curtain-close signature: top and bottom bands darken significantly faster
    than the middle band in frames leading up to the black segment.

    Fade-to-black signature: all three bands darken at roughly the same rate.

    Args:
        video_path: Path to the video file.
        black_segments: List of dicts from detect_black_segments(), each with
                        {start_s, end_s, duration_s}.

    Returns:
        The same list with a "likely_type" field added to each segment:
        "curtain-close", "fade-black-hold", or "unknown".
    """
    if not black_segments:
        return black_segments

    for seg in black_segments:
        seg["likely_type"] = _classify_single_segment(video_path, seg)

    classified_types = [s["likely_type"] for s in black_segments]
    log.info(
        "black_segment_classification_done",
        total=len(black_segments),
        curtain_close=classified_types.count("curtain-close"),
        fade_black=classified_types.count("fade-black-hold"),
        unknown=classified_types.count("unknown"),
    )

    return black_segments


def _classify_single_segment(
    video_path: str,
    segment: dict,
) -> str:
    """Classify a single black segment by analyzing pre-segment frames.

    Samples frames in the window before black_start and checks whether
    edge bands (top + bottom) darken faster than the middle band.
    """
    black_start = segment["start_s"]
    pre_window_start = max(0.0, black_start - _CLASSIFY_PRE_WINDOW_S)

    # Not enough pre-segment time to analyze (e.g. black at video start)
    if black_start < 0.1:
        return "unknown"

    # Sample timestamps: evenly spaced in the pre-window, chronological order
    available_window = black_start - pre_window_start
    if available_window < 0.05:
        return "unknown"

    step = available_window / _CLASSIFY_SAMPLE_COUNT
    timestamps = [pre_window_start + step * i for i in range(_CLASSIFY_SAMPLE_COUNT)]

    # Collect luminance bands for each sample frame
    frame_bands: list[dict[str, float]] = []
    for ts in timestamps:
        bands = _sample_frame_bands(video_path, ts)
        if bands is not None:
            frame_bands.append(bands)

    if len(frame_bands) < 3:
        log.debug("classify_insufficient_frames", segment_start=black_start)
        return "unknown"

    # Analyze the pattern: count frames where edges are darker than middle
    # Focus on the LATER frames (closer to black_start) where the effect is strongest
    later_frames = frame_bands[len(frame_bands) // 2:]
    bar_pattern_count = 0

    for bands in later_frames:
        edge_avg = (bands["top"] + bands["bottom"]) / 2.0
        middle = bands["middle"]

        # Avoid division by zero — if edges are near-black, check the ratio
        if edge_avg < 1.0:
            # Edges are already black; if middle is still bright, strong bar pattern
            if middle > 30.0:
                bar_pattern_count += 1
        elif middle / edge_avg >= _CURTAIN_CLOSE_RATIO_THRESHOLD:
            bar_pattern_count += 1

    if bar_pattern_count >= _CURTAIN_CLOSE_MIN_FRAMES:
        log.info(
            "curtain_close_detected",
            segment_start=round(black_start, 2),
            bar_frames=bar_pattern_count,
            total_later_frames=len(later_frames),
        )
        return "curtain-close"

    # Check for uniform darkening (fade-to-black pattern)
    # Compare first and last frame — if all bands dropped by similar amounts, it's a fade
    first = frame_bands[0]
    last = frame_bands[-1]

    first_avg = (first["top"] + first["middle"] + first["bottom"]) / 3.0
    last_avg = (last["top"] + last["middle"] + last["bottom"]) / 3.0

    if first_avg > 30.0 and last_avg < first_avg * 0.5:
        # Significant overall darkening occurred — check uniformity
        top_drop = first["top"] - last["top"]
        mid_drop = first["middle"] - last["middle"]
        bot_drop = first["bottom"] - last["bottom"]

        drops = [d for d in (top_drop, mid_drop, bot_drop) if d > 0]
        if drops:
            max_drop = max(drops)
            min_drop = min(drops)
            # Uniform if max/min ratio is close to 1
            if max_drop > 0 and min_drop / max_drop > 0.5:
                return "fade-black-hold"

    return "unknown"
