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
import re
import subprocess

import structlog

from app.config import settings
from app.pipeline.audio_layout import (
    BODY_SLOT_AUDIO_OUT_ARGS,
    SILENT_AUDIO_INPUT_ARGS,
)
from app.pipeline.probe import probe_video
from app.pipeline.text_overlay import FONTS_DIR

log = structlog.get_logger()

MAX_OUTPUT_BYTES = 500 * 1024 * 1024  # 500MB

# Backward-compat alias — older code referenced the local constant.
_SILENT_AUDIO_INPUT = SILENT_AUDIO_INPUT_ARGS


class ReframeError(Exception):
    pass


def _double_rate(rate: str) -> str:
    """Double an ffmpeg bitrate string (e.g. "12M" -> "24M", "800K" -> "1600K").

    Used to derive the VBV bufsize from the maxrate. A bufsize of ~2× maxrate
    gives x264 enough of a buffer window to smooth complexity spikes (the bright
    sunset band) without letting the average blow past the ceiling. Falls back
    to the input unchanged if the format is unexpected.
    """
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KkMmGg]?)", rate.strip())
    if not match:
        return rate
    value = float(match.group(1)) * 2
    suffix = match.group(2).upper()
    # Drop a trailing ".0" so "4M" -> "8M" not "8.0M".
    num = f"{value:g}"
    return f"{num}{suffix}"


def _encoding_args(
    output_path: str,
    preset: str = "fast",
    crf: str = "18",
    include_audio: bool = True,
) -> list[str]:
    """Shared FFmpeg output encoding arguments (DRY).

    Args:
        preset: libx264 preset. Use "ultrafast" for intermediate files
                that will be re-encoded later, "fast" for final output.
        crf: Constant Rate Factor. Lower = better quality.
             18 is visually lossless, 23 is default (too lossy for
             multi-pass pipelines with 3+ re-encodes).

    Rate control: capped CRF, NOT ABR. We pass `-crf` + `-maxrate` + `-bufsize`
    and deliberately do NOT pass `-b:v`. Passing `-b:v` forces x264 into ABR
    mode, which IGNORES `-crf` entirely and holds a flat average bitrate — that
    starves low-complexity-but-gradient regions (a dark twilight sky next to a
    bright sunset band) and macroblocks them. Capped CRF means "CRF `crf`
    quality unless it would exceed `settings.output_video_bitrate`," so smooth
    skies stay clean while the ceiling still bounds file size. `bufsize` is
    derived as 2× the ceiling (`_double_rate`) — the old hardcoded "8M" against
    a 4M maxrate was a 2× window by accident and broke the moment the ceiling
    moved. History: prod job rendering a 10-bit HLG iPhone sunset
    (lisbon1.MOV, 2026-05-25) showed 16×16 blocking in the dark sky because the
    4M `-b:v` ABR ceiling, not CRF 18, governed the encode.

    Color handling: HDR/HLG sources (bt2020, arib-std-b67) are converted
    to SDR (bt709) via the colorspace filter in _build_video_filter. The
    10→8-bit downconvert there uses error-diffusion dither (_ZSCALE_SDR_PIPELINE)
    to avoid contour banding on smooth gradients. This function tags the output
    as bt709 so players render colors correctly.

    Preset policy (locked by tests/test_encoder_policy.py — audit before
    flipping any of these):

      INTERMEDIATE  (preset="ultrafast" — fine, re-encoded downstream)
        - reframe_and_export()           reframe.py:228, 376
        - image_clip rendering           image_clip.py:111, 217
        - render_color_hold()            interstitials.py:121
        - drive_import thumbnailing      drive_import.py:74

      INTERMEDIATE — does NOT route through _encoding_args (intentionally)
        - normalize_orientation()        orientation.py — needs in-place
          dimension preservation, so cannot use _encoding_args (which
          force-rescales to 1080x1920). Inline ultrafast libx264 args
          documented in orientation.py.

      FINAL OUTPUT (preset="fast" — banding-sensitive, must NOT regress)
        - _concat_demuxer fallback       template_orchestrate.py:2117
        - _pre_burn_curtain_slot_text    template_orchestrate.py:2558
        - apply_curtain_close_tail       interstitials.py:250 (+ tune=film)
        - join_with_transitions          transitions.py:110
        - _burn_text_overlays            template_orchestrate.py:2671
        - build_talking_head_command     talking_head_assembler.py (Lane C)

    Why this matters: libx264 preset=ultrafast disables mb-tree, psy-rd,
    B-frames and trellis quant. On smooth gradients (sky, dark canopy)
    those losses are visible as 16×16 macroblocking. CRF does NOT save
    you — CRF controls the rate target; preset controls which encoder
    features run. ultrafast at CRF 18 still produces blocky output.

    History of the same bug:
      - PR #102: curtain-close ultrafast → medium      (banding fix)
      - PR #105: curtain-close medium → fast +         (perf)
                  --concurrency=1 (fly.toml) +
                  PNG-overlay (10× faster than geq)
      - Brazil pixelation PR: propagated preset=fast   (this fix)
                  to the three template_orchestrate.py
                  sites that PR #102/#105 missed.
    """
    args = [
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-preset",
        preset,
        "-crf",
        crf,
        "-pix_fmt",
        "yuv420p",
        # ── Closed-GOP, no-B-frame encoding for clean concat boundaries ────
        # Each slot is encoded independently then joined by `_concat_demuxer`
        # with `-c copy` (PR #87). Without these args, libx264 produces
        # open-GOP H.264 with B-frames at the very start/end of each clip;
        # at concat boundaries the decoder interpolates between the last
        # reference frame of clip A and the first I-frame of clip B,
        # producing a 1-frame alpha-blend ("crossfade") artifact at every
        # hard cut. Users perceive this as a "lag/bug at scene start."
        # `-bf 0` disables B-frames entirely; `keyint=30:scenecut=0:open_gop=0`
        # forces an I-frame at clip start with no scene-detected mid-clip
        # I-frames. File-size cost: ~5-10% larger per slot; visual cost: zero.
        "-bf",
        "0",
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
        args += ["-x264-params", "scenecut=40:keyint=90:open_gop=0:bframes=0"]
    else:
        # Intermediate (ultrafast) slots: enforce closed-GOP + no B-frames
        # explicitly. ultrafast turns these off in its preset defaults but
        # being explicit guarantees behavior across libx264 versions.
        args += ["-x264-params", "scenecut=0:open_gop=0:bframes=0"]
    args += [
        # Tag output as bt709 (SDR) so players render colors correctly.
        # iPhone clips often come as bt2020/HLG; without explicit tagging
        # the encoder copies source tags → player misinterprets SDR data
        # as HDR → washed-out look.
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        # Capped CRF, not ABR — see the rate-control note in the docstring.
        # `-crf` (set above) is the quality driver; `-maxrate`/`-bufsize` are the
        # ceiling. No `-b:v` here, on purpose.
        "-maxrate",
        settings.output_video_bitrate,
        "-bufsize",
        _double_rate(settings.output_video_bitrate),
        "-r",
        str(settings.output_fps),
    ]
    if include_audio:
        # Force identical body-slot audio layout (44.1kHz stereo AAC 192k) so
        # the downstream concat can stream-copy. See app/pipeline/audio_layout.py
        # for the contract — drift here re-introduces the silent-truncation bug.
        # Video-only encodes (e.g. join_with_transitions, which mixes template
        # audio separately and passes -an) set include_audio=False to skip these.
        args += [*BODY_SLOT_AUDIO_OUT_ARGS]
    args += [
        "-s",
        f"{settings.output_width}x{settings.output_height}",
        "-movflags",
        "+faststart",
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
    has_grid: bool = False,
    grid_color: str = "#FFFFFF",
    grid_opacity: float = 0.6,
    grid_thickness: int = 3,
    grid_highlight_intersection: str | None = None,
    grid_highlight_color: str = "#E63946",
    grid_highlight_windows: list[tuple[float, float]] | None = None,
    color_trc: str | None = None,
    has_audio: bool | None = None,
    keep_segments: list[tuple[float, float]] | None = None,
    keep_segments_punch_in: float | None = None,
) -> None:
    """Render a single clip to the output spec. Raises ReframeError on failure.

    Never uses shell=True. All paths are passed as list args.

    text_overlay_pngs: list of {png_path, start_s, end_s} for PNG overlay compositing.
    ass_overlay_paths: list of ASS file paths for animated text overlays.
    speed_factor: playback speed multiplier (2.0 = 2x fast). Applied as setpts=PTS/speed_factor.
    color_hint: color grading preset -- "warm", "cool", "high-contrast", etc.
    keep_segments: optional [(start_s, end_s), ...] in the OUTPUT clip timeline
        (post -ss/-t, 0-based) — render only these spans, concatenated in
        order, with 5ms audio declick fades at cut-adjacent edges
        (plans/010 silence cut). Validated fail-loud (sorted,
        non-overlapping, within [0, duration]); raises ValueError on
        violation — callers own the uncut fallback. Not combinable with
        text_overlay_pngs / ass_overlay_paths: cut first, then burn overlays
        on the cut output. None ⇒ byte-identical to the uncut command.
    """
    duration = end_s - start_s
    if duration <= 0:
        raise ReframeError(f"Invalid duration: {duration}s")

    # Probe color transfer characteristic to select the right HDR→SDR filter.
    # Caller may pre-probe and pass color_trc to skip a redundant ffprobe per
    # slot (multi-slot templates probe the same source up to N times otherwise).
    # has_audio is read from the same probe — used to decide whether to inject
    # a silent AAC track so the reframed slot has the same audio layout as
    # every other slot (preconditions for stream-copy concat downstream).
    if color_trc is None or has_audio is None:
        try:
            clip_probe = probe_video(input_path)
            if color_trc is None:
                color_trc = clip_probe.color_trc
            if has_audio is None:
                has_audio = clip_probe.has_audio
        except Exception:
            if color_trc is None:
                color_trc = "bt709"
            if has_audio is None:
                has_audio = False

    # Build video filter chain
    vf_parts = _build_video_filter(
        aspect_ratio,
        ass_subtitle_path,
        color_hint=color_hint,
        speed_factor=speed_factor,
        darkening_windows=darkening_windows,
        narrowing_windows=narrowing_windows,
        color_trc=color_trc,
        output_fit=output_fit,
        has_grid=has_grid,
        grid_color=grid_color,
        grid_opacity=grid_opacity,
        grid_thickness=grid_thickness,
        grid_highlight_intersection=grid_highlight_intersection,
        grid_highlight_color=grid_highlight_color,
        grid_highlight_windows=grid_highlight_windows,
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

    if keep_segments is not None:
        if has_overlays:
            raise ValueError(
                "keep_segments cannot be combined with text_overlay_pngs/"
                "ass_overlay_paths — cut first, then burn overlays on the "
                "cut output (plans/010: captions are rebuilt from remapped "
                "words in the cut timeline)"
            )
        _validate_keep_segments(keep_segments, duration)

    if has_overlays:
        cmd = _build_overlay_cmd(
            input_path,
            start_s,
            duration,
            vf_parts,
            text_overlay_pngs or [],
            ass_overlay_paths or [],
            output_path,
            has_audio=has_audio,
        )
    elif keep_segments is not None:
        log.info(
            "reframe_keep_segments",
            segments=len(keep_segments),
            kept_s=round(sum(b - a for a, b in keep_segments), 3),
            duration_s=duration,
        )
        # Punch-in needs the chain's exact output dims so every segment
        # returns to identical geometry before concat (mixed dims abort).
        punch_dims = (
            (settings.output_width, settings.output_height)
            if aspect_ratio == "9:16"
            else (settings.output_height, settings.output_width)
        )
        cmd = _build_keep_segments_cmd(
            input_path,
            start_s,
            duration,
            vf_parts,
            keep_segments,
            has_audio=has_audio,
            punch_in=keep_segments_punch_in,
            punch_dims=punch_dims,
        )
        # Same intermediate ultrafast/crf=14 budget as the uncut branch below —
        # the cut output is re-encoded by the downstream burn exactly like an
        # uncut reframe. The call stays in THIS function (not the builder) so
        # the encoder-policy audit (tests/test_encoder_policy.py) keeps seeing
        # only allowlisted call sites.
        cmd += _encoding_args(output_path, preset="ultrafast", crf="14")
    else:
        vf_string = ",".join(vf_parts)
        cmd = ["ffmpeg", "-ss", str(start_s), "-t", str(duration), "-i", input_path]
        if not has_audio:
            # Silent-audio input MUST come after -ss/-t so the source seek
            # doesn't apply to the lavfi source. -shortest then truncates
            # the silent track to the trimmed video duration.
            cmd += _SILENT_AUDIO_INPUT
            cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        cmd += [
            "-vf",
            vf_string,
            # Intermediate (re-encoded by the final burn). ultrafast keeps per-slot
            # render speed, but its weaker tools macroblock dark gradients and the
            # final fast pass can't recover that. crf=14 (vs the 18 default) spends
            # more bits here so the temp file stays clean for the final encode —
            # ~38% less dark-region distortion in local A/B (job 792f2d52). Temp-only;
            # shipped bytes still come from the final fast pass.
            *_encoding_args(output_path, preset="ultrafast", crf="14"),
        ]

    log.info(
        "reframe_start",
        input=input_path,
        start=start_s,
        end=end_s,
        aspect=aspect_ratio,
        speed=speed_factor,
        overlays=len(text_overlay_pngs or []),
        ass_overlays=len(ass_overlay_paths or []),
    )

    # 1500s caps a single-slot ffmpeg invocation just below the parent Celery
    # soft time limit (1740s on orchestrate_template_job — see template_orchestrate.py).
    # Prior cap was 600s, which fired twice in a row on 4K iPhone HDR sources
    # (jobs 4551074a + 343af2dc, template 77151144, 2026-05-15) while
    # format=gbrpf32le was burning bandwidth at native 4K. With PR2's filter
    # reorder a 24s 4K HLG clip lands in ~4-5 min; this raised cap is a safety
    # net for older HDR fixtures (longer clips, 60fps HDR, multi-overlay slots)
    # where the prior 600s had no head-room and the parent orchestrator was
    # silently swallowing TimeoutExpired and marking the whole job failed.
    result = subprocess.run(cmd, capture_output=True, timeout=1500, check=False)

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
            has_grid=has_grid,
            grid_color=grid_color,
            grid_opacity=grid_opacity,
            grid_thickness=grid_thickness,
            grid_highlight_intersection=grid_highlight_intersection,
            grid_highlight_color=grid_highlight_color,
            grid_highlight_windows=grid_highlight_windows,
            color_trc=color_trc,
            has_audio=has_audio,
            keep_segments=keep_segments,
        )

    if result.returncode != 0:
        # FFmpeg always prints a long version + configuration banner to stderr.
        # The actual error lives in the lines that *don't* match those headers.
        # Drop them so we surface the real failure even when the banner alone
        # exceeds our truncation buffer.
        full_stderr = result.stderr.decode(errors="replace")
        meaningful = [
            ln
            for ln in full_stderr.splitlines()
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
    has_audio: bool = True,
) -> list[str]:
    """Build FFmpeg command with -filter_complex for PNG + ASS overlay compositing.

    Chain: [0:v] -> vf filters -> overlay each PNG -> subtitles each ASS -> output
    """
    cmd = [
        "ffmpeg",
        "-ss",
        str(start_s),
        "-t",
        str(duration),
        "-i",
        input_path,
    ]

    # Add each PNG as an input
    for ov in overlay_pngs:
        cmd.extend(["-i", ov["png_path"]])

    # Silent-audio fallback input — placed AFTER PNGs so the audio map index
    # is predictable: 1 + len(overlay_pngs) + len(ass_overlay_paths-as-inputs).
    # ASS overlays go through the subtitles filter (not -i inputs), so the
    # silent track sits at index 1 + len(overlay_pngs).
    silent_audio_idx = None
    if not has_audio:
        silent_audio_idx = 1 + len(overlay_pngs)
        cmd.extend(_SILENT_AUDIO_INPUT)

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
        fc_parts.append(f"[{prev_label}]subtitles={escaped}:fontsdir={escaped_fonts}[{out_label}]")
        prev_label = out_label

    filter_complex = ";".join(fc_parts)

    cmd.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            f"[{prev_label}]",
        ]
    )
    if has_audio:
        cmd.extend(["-map", "0:a?"])
    else:
        cmd.extend(["-map", f"{silent_audio_idx}:a:0", "-shortest"])
    # Intermediate (re-encoded downstream). crf=14 keeps this overlay-on-base temp
    # file clean so the final pass isn't fed pre-baked dark-gradient blocking. See
    # the matching note in reframe_and_export.
    cmd.extend(_encoding_args(output_path, preset="ultrafast", crf="14"))

    return cmd


# 5ms declick fade at cut-adjacent keep-segment edges (plans/010, eng review
# T3=C). Long enough to kill the step discontinuity (click) an arbitrary
# waveform cut produces, short enough to be inaudible as a fade. Never applied
# at the clip's true start/end — those edges are not cuts.
# 12ms: still imperceptible as a fade but kills boundary clicks more reliably
# than 5ms on real phone audio (local-test round 2, 2026-07-09).
_DECLICK_FADE_S = 0.012


def _validate_keep_segments(
    keep_segments: list[tuple[float, float]],
    duration: float,
) -> None:
    """Fail-loud validation for reframe_and_export(keep_segments=...).

    Callers own the fallback (render uncut) — a malformed cut plan must never
    silently render a wrong cut, so any violation raises ValueError.
    """
    if not keep_segments:
        raise ValueError("keep_segments must contain at least one segment")
    prev_end = 0.0
    for i, (seg_start, seg_end) in enumerate(keep_segments):
        if not seg_start < seg_end:
            raise ValueError(
                f"keep_segments[{i}] has non-positive length: ({seg_start}, {seg_end})"
            )
        if seg_start < 0 or seg_end > duration:
            raise ValueError(
                f"keep_segments[{i}] out of bounds [0, {duration}]: ({seg_start}, {seg_end})"
            )
        if i > 0 and seg_start < prev_end:
            raise ValueError(
                f"keep_segments must be sorted and non-overlapping; segment {i} "
                f"starts at {seg_start} before previous segment end {prev_end}"
            )
        prev_end = seg_end


def _build_keep_segments_cmd(
    input_path: str,
    start_s: float,
    duration: float,
    vf_parts: list[str],
    keep_segments: list[tuple[float, float]],
    has_audio: bool = True,
    punch_in: float | None = None,
    punch_dims: tuple[int, int] | None = None,
) -> list[str]:
    """Build the FFmpeg command prefix for keep-segment cutting (plans/010).

    The cut runs INSIDE the reframe filtergraph, appended AFTER the existing
    scale/crop/fps chain (vf_parts, which always includes the
    `framerate=fps=N` CFR stage) — so frame math only ever touches the
    CFR-normalized stream, never raw phone VFR/HEIF input with
    `avg_frame_rate=1/0` (the CFR-before-xfade incident class; see
    single_pass.py and agents/DECISIONS.md 2026-05-18).

    Chain: [0:v] -> vf filters -> [base] -> split -> per-segment
    trim + setpts=PTS-STARTPTS; audio -> asplit -> per-segment
    atrim + asetpts=PTS-STARTPTS + 5ms afade declick at CUT-ADJACENT edges
    only (a segment starting at the clip's true start gets no fade-in; one
    ending at the clip's true end gets no fade-out) -> concat. Per-segment
    concat re-syncs A/V at every joint (shorter audio is silence-padded to
    the segment max), so cumulative drift is structurally impossible — the
    30-cut drift e2e in tests/pipeline/test_reframe_keep_segments.py is the
    permanent guard.

    Sources with no audio track mirror the uncut silent-audio contract:
    lavfi anullsrc input after the main input, concat v=1:a=0, and
    `-map 1:a:0 -shortest` truncating the silent track to the cut video.

    Returns the command WITHOUT output encoding args — the caller appends
    `_encoding_args(...)` itself, keeping every encoder-policy call site
    inside reframe_and_export (tests/test_encoder_policy.py allowlist).
    """
    n = len(keep_segments)
    cmd = ["ffmpeg", "-ss", str(start_s), "-t", str(duration), "-i", input_path]
    if not has_audio:
        cmd += _SILENT_AUDIO_INPUT

    vf_chain = ",".join(vf_parts) if vf_parts else "null"
    fc_parts = [f"[0:v]{vf_chain}[base]"]

    # Alternating punch-in (plans/010 local-test round 2): odd segments get a
    # subtle zoom so each cut reads as an intentional framing change (the
    # standard talking-head jump-cut idiom) instead of a positional stutter.
    # Scale up by the factor, then crop back to the chain's EXACT output dims
    # so all segments share geometry (concat aborts on mixed dims).
    punch_chain = ""
    if punch_in is not None and punch_in > 1.0 and punch_dims is not None:
        base_w, base_h = punch_dims
        punch_w = int(round(base_w * punch_in / 2)) * 2
        punch_h = int(round(base_h * punch_in / 2)) * 2
        punch_chain = f",scale={punch_w}:{punch_h},crop={base_w}:{base_h}"

    split_out = "".join(f"[vs{i}]" for i in range(n))
    fc_parts.append(f"[base]split={n}{split_out}")
    for i, (seg_start, seg_end) in enumerate(keep_segments):
        seg_punch = punch_chain if i % 2 == 1 else ""
        fc_parts.append(
            f"[vs{i}]trim=start={seg_start:.6f}:end={seg_end:.6f},"
            f"setpts=PTS-STARTPTS{seg_punch}[v{i}]"
        )

    if has_audio:
        asplit_out = "".join(f"[as{i}]" for i in range(n))
        fc_parts.append(f"[0:a]asplit={n}{asplit_out}")
        for i, (seg_start, seg_end) in enumerate(keep_segments):
            chain = f"atrim=start={seg_start:.6f}:end={seg_end:.6f},asetpts=PTS-STARTPTS"
            if seg_start > 0:
                chain += f",afade=t=in:st=0:d={_DECLICK_FADE_S}"
            if seg_end < duration:
                fade_out_start = (seg_end - seg_start) - _DECLICK_FADE_S
                chain += f",afade=t=out:st={fade_out_start:.6f}:d={_DECLICK_FADE_S}"
            fc_parts.append(f"[as{i}]{chain}[a{i}]")
        pairs = "".join(f"[v{i}][a{i}]" for i in range(n))
        fc_parts.append(f"{pairs}concat=n={n}:v=1:a=1[vout][aout]")
    else:
        vids = "".join(f"[v{i}]" for i in range(n))
        fc_parts.append(f"{vids}concat=n={n}:v=1:a=0[vout]")

    cmd += ["-filter_complex", ";".join(fc_parts), "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "[aout]"]
    else:
        cmd += ["-map", "1:a:0", "-shortest"]
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
#
# Pre-downscale BEFORE format=gbrpf32le, in linear-light 10-bit YUV space. The
# `format=gbrpf32le` step is a 6.4× memory expansion (10-bit YUV → 32-bit float
# planar) — for a 4K iPhone HDR source that's ~95MB/frame of bandwidth. PR #152
# put the scale AFTER format=gbrpf32le, which still left the heavy float upconvert
# running on 4K frames; reframe_and_export hit the 600s subprocess timeout again
# on job 343af2dc... (template 77151144..., 2026-05-15, 10m43s elapsed) even
# with the new scale in place. Moving scale ABOVE format=gbrpf32le means the
# float upconvert only runs on already-downscaled 1080p frames (~24MB/frame,
# 4× less bandwidth, 4× less CPU). `scale` operates fine on 10-bit YUV — only
# `tonemap` requires float input, and `tonemap` still sits AFTER format=gbrpf32le.
#
# Scaling in linear-light is also the physically correct HDR-aware order:
# averaging gamma-encoded HDR luminance biases bright values; averaging linear
# light averages physical luminance. `force_original_aspect_ratio=decrease`
# makes this a strict downscale — a 1080×1920 HDR source passes through
# unchanged. lanczos preserves highlight detail across the downscale (bilinear,
# the scale default, blurs specular highlights). Expected wall-clock breakdown
# for a 24s 4K HLG clip on shared-cpu-2x: HEVC decode ~60s, zscale=t=linear at
# 4K ~30s, scale 4K→1080p YUV ~20s, format=gbrpf32le at 1080p ~75s, gamut +
# tonemap + transfer at 1080p ~50s, libx264 ultrafast encode at 1080p ~30s =
# ~4-5 min total, well under the 600s subprocess cap.
_ZSCALE_SDR_PIPELINE = (
    "zscale=t=linear:npl=400"
    f",scale={settings.output_height}:{settings.output_height}"
    ":force_original_aspect_ratio=decrease:flags=lanczos"
    ",format=gbrpf32le"
    ",zscale=p=bt709"
    ",tonemap=tonemap=mobius"
    # dither=error_diffusion: the only place a 10-bit HDR gradient collapses to
    # 8-bit. Without error diffusion this stair-steps into visible contour
    # bands on smooth skies (zscale defaults to dither=none). Error diffusion
    # spreads quantization error into adjacent pixels so the gradient reads
    # smooth. SDR (bt709) sources never reach this pipeline, so this is a no-op
    # for non-HDR footage. History: lisbon1.MOV HLG sunset, 2026-05-25.
    ",zscale=t=bt709:m=bt709:r=tv:dither=error_diffusion"
    ",format=yuv420p"
)


# Local-dev fallback when ffmpeg lacks libzimg (no zscale filter).
# The default Homebrew ffmpeg formula does not enable libzimg; production Docker
# does. Without zscale we can't run the proper HLG→SDR tonemap, so this path
# strips HDR metadata, forces yuv420p, and tags the output bt709. Colors will
# look slightly washed out compared to the production tonemapped output, but
# the pipeline runs end-to-end so editor previews and test jobs work locally.
_HDR_FALLBACK_PIPELINE = "format=yuv420p"


def _zscale_available() -> bool:
    """Cached check for whether the local ffmpeg has the zscale filter.

    Production always has it (built into the Docker image). Local Homebrew
    ffmpeg often does not. Run once per process; the binary is unlikely to
    swap underneath us.
    """
    if hasattr(_zscale_available, "_cached"):
        return _zscale_available._cached  # type: ignore[attr-defined]
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        available = "zscale" in (result.stdout or "")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        available = False
    _zscale_available._cached = available  # type: ignore[attr-defined]
    if not available:
        log.warning(
            "zscale_filter_unavailable",
            hint=(
                "Local ffmpeg lacks libzimg; HDR/HLG sources will use a "
                "best-effort fallback (no proper tonemap). Production Docker "
                "is unaffected."
            ),
        )
    return available


def _is_landscape(probe: object) -> bool:
    """Return True when the source clip is wider than tall.

    Prefers real pixel dimensions (probe.width / probe.height); falls back to
    probe.aspect_ratio == "16:9" when the probe is absent or dims are zero.
    """
    try:
        w = int(getattr(probe, "width", 0) or 0)
        h = int(getattr(probe, "height", 0) or 0)
        if w > 0 and h > 0:
            return w > h
    except (TypeError, ValueError):
        pass
    return getattr(probe, "aspect_ratio", "") == "16:9"


def resolve_output_fit(
    probe: object,
    *,
    landscape_fit: str = "fill",
    default_fit: str = "crop",
) -> str:
    """Per-clip output_fit resolution for the landscape-fit user preference.

    When the user chose ``landscape_fit="fit"`` AND the source is wider than
    tall, returns ``"letterbox_black"`` — full-width, black bars top & bottom,
    no zoom.  All other cases (portrait, square, landscape with ``"fill"``
    preference) return ``default_fit`` unchanged, so callers that don't pass
    the arg stay byte-identical to today's crop behavior.

    ``landscape_fit`` defaults to ``"fill"`` so template/music callers that
    don't pass the argument remain byte-identical to the current crop
    behavior.
    """
    if landscape_fit == "fit" and _is_landscape(probe):
        return "letterbox_black"
    return default_fit


def _build_video_filter(
    aspect_ratio: str,
    ass_path: str | None,
    color_hint: str = "none",
    speed_factor: float = 1.0,
    darkening_windows: list[tuple[float, float]] | None = None,
    narrowing_windows: list[tuple[float, float]] | None = None,
    color_trc: str = "bt709",
    output_fit: str = "crop",
    has_grid: bool = False,
    grid_color: str = "#FFFFFF",
    grid_opacity: float = 0.6,
    grid_thickness: int = 3,
    grid_highlight_intersection: str | None = None,
    grid_highlight_color: str = "#E63946",
    grid_highlight_windows: list[tuple[float, float]] | None = None,
) -> list[str]:
    """Return list of filter segments to join with commas.

    Filter order: HDR tonemap (if needed) -> setpts (speed) -> scale/crop
                  -> color grading -> darkening -> narrowing
                  -> grid (base) -> grid-highlight -> caption ASS.
    setpts MUST be first to normalize PTS before timed filters (darkening, narrowing).
    Text overlays are handled separately via overlay filter (not in this chain).
    Grid (rule-of-thirds) is drawn after visual adjustments but before captions so it
    sits above the footage and behind any text overlays burned in post-assembly.

    Grid-highlight is a per-intersection accent: when an intersection is given,
    the ONE vertical + ONE horizontal line bordering that corner are drawn in
    highlight_color on top of the white base grid, but only during the given
    (start_s, end_s) windows. This matches the Rule-of-Thirds template aesthetic
    where the line pair pointing at the subject's compositional position turns
    red on every beat.
    """
    filters: list[str] = []

    # 0a. HDR/HLG → SDR color conversion.
    # HLG (arib-std-b67) and HDR10 (smpte2084) require zscale tonemap pipeline —
    # FFmpeg's `colorspace` filter rejects these transfer characteristics.
    # bt709 clips (Android SDR, screen recordings) use colorspace=all=bt709 (no-op for SDR).
    # When zscale is missing (local Homebrew ffmpeg without libzimg), fall back
    # to a no-tonemap path so the pipeline still produces output for dev testing.
    if color_trc in (_HLG_TRANSFER, _HDR10_TRANSFER):
        if _zscale_available():
            filters.append(_ZSCALE_SDR_PIPELINE)
        else:
            filters.append(_HDR_FALLBACK_PIPELINE)
    else:
        # iall=bt709: assume bt709 input even when primaries/matrix are tagged
        # "unknown" in the SPS. libx264's color_primaries/colorspace metadata
        # doesn't propagate from FFmpeg's muxer flags without -x264-params, so
        # photo→mp4 (image_to_video.py) and some phone recordings ship with
        # primaries=unknown. The colorspace filter returns EINVAL on unknown
        # primaries — empirically observed as rc=234 in prod. Mirrors the
        # existing color_trc unknown→bt709 fallback at reframe.py:172.
        filters.append("colorspace=all=bt709:iall=bt709")

    # 0b. Frame-rate normalization to output fps. Many user-uploaded clips
    # are not 30fps — iPhone "cinematic" defaults to 23.976fps and some
    # Android / PAL-region phones record at 25fps. The output container is
    # 30fps. Without an explicit fps filter, ffmpeg's default vsync=cfr at
    # the `-r 30` muxer flag (see _encoding_args) duplicates frames at
    # uniform positions (every 5th frame for 24→30, every 6th for 25→30).
    # That produces a regular visible stutter (6Hz for 24fps source) that
    # analyze_scene_glitches.py confirmed on slots 3 and 6 of the Dimples
    # render. `fps=30` filter run at the start of the chain gives ffmpeg
    # the same duplication policy but earlier in the pipeline; the real
    # quality bump comes from using motion-blending via `framerate`, which
    # produces interpolated frames at the new timestamps instead of pure
    # duplicates — so consecutive frames always carry SOME motion and no
    # frame is pixel-identical to its predecessor. For 30fps sources this
    # filter is essentially a passthrough (zero new frames to synthesize).
    filters.append(f"framerate=fps={settings.output_fps}")

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
        filters.append(f"scale={ow}:{oh}:force_original_aspect_ratio=decrease")
        filters.append(f"pad={ow}:{oh}:(ow-iw)/2:(oh-ih)/2:color=black")
    elif aspect_ratio == "16:9":
        filters.append(f"scale=-2:{settings.output_height}")
        filters.append(f"crop={settings.output_width}:{settings.output_height}")
    else:
        # Default crop mode: scale-fill the canvas (no bars), trim overflow.
        # `force_original_aspect_ratio=increase` grows BOTH source dims until
        # they cover the target, preserving aspect; `crop` center-trims the
        # overflow to exact target dims. Mirrors the 16:9 branch above
        # (`scale=-2:H,crop=W:H`) for non-16:9 sources — covers 3:4 portrait,
        # 19.5:9 iPhone, square, slightly-off-9:16, etc. Callers that WANT
        # black bars (e.g. locked 16:9 templates with baked-in hook text on
        # the sides) MUST pass `output_fit="letterbox_black"` to hit the
        # branch above; do not rely on this branch padding.
        filters.append(
            f"scale={settings.output_width}:{settings.output_height}"
            f":force_original_aspect_ratio=increase"
        )
        filters.append(f"crop={settings.output_width}:{settings.output_height}")

    # 2. Color grading (applied before darkening so darkened areas have the grade)
    for f in _color_hint_filters(color_hint):
        filters.append(f)

    # 3. Darkening (colorlevels with enable timing)
    for start, end in darkening_windows or []:
        filters.append(
            f"colorlevels=rimax=0.45:gimax=0.45:bimax=0.45"
            f":enable='between(t,{start:.3f},{end:.3f})'"
        )

    # 4. Narrowing (letterbox drawbox bars, top + bottom)
    for start, end in narrowing_windows or []:
        enable = f"enable='between(t,{start:.3f},{end:.3f})'"
        filters.append(f"drawbox=x=0:y=0:w=iw:h=120:color=black@0.85:t=fill:{enable}")
        filters.append(f"drawbox=x=0:y=ih-120:w=iw:h=120:color=black@0.85:t=fill:{enable}")

    # 5. Rule-of-thirds grid overlay (per-slot)
    if has_grid:
        # Draw only the 4 INNER lines (2 vertical at iw/3 and 2*iw/3,
        # 2 horizontal at ih/3 and 2*ih/3). FFmpeg's drawgrid would also
        # render the outer frame at x=0/iw and y=0/ih which doesn't belong
        # to the rule-of-thirds grid — explicit drawbox per inner line
        # avoids that. Stroke is centered on the line via half-thickness.
        color_hex = grid_color.lstrip("#")
        half_g = grid_thickness // 2
        for v_x in ("iw/3", "2*iw/3"):
            filters.append(
                f"drawbox=x={v_x}-{half_g}:y=0:w={grid_thickness}:h=ih"
                f":color=0x{color_hex}@{grid_opacity:.2f}:t=fill"
            )
        for h_y in ("ih/3", "2*ih/3"):
            filters.append(
                f"drawbox=x=0:y={h_y}-{half_g}:w=iw:h={grid_thickness}"
                f":color=0x{color_hex}@{grid_opacity:.2f}:t=fill"
            )

        # 5b. Per-intersection highlight on top of the base grid.
        # Two drawbox calls render the vertical + horizontal line that border
        # the chosen rule-of-thirds intersection corner. The highlight is drawn
        # 3x thicker than the base grid so the red L visually dominates the
        # white grid at the ~3:1 ratio measured from the reference TikTok.
        # When no time windows are given the highlight is on for the whole
        # slot; otherwise it's gated.
        if grid_highlight_intersection:
            _v_line_x = {
                "top-left": "iw/3",
                "bottom-left": "iw/3",
                "top-right": "2*iw/3",
                "bottom-right": "2*iw/3",
            }
            _h_line_y = {
                "top-left": "ih/3",
                "top-right": "ih/3",
                "bottom-left": "2*ih/3",
                "bottom-right": "2*ih/3",
            }
            v_x = _v_line_x[grid_highlight_intersection]
            h_y = _h_line_y[grid_highlight_intersection]

            highlight_hex = grid_highlight_color.lstrip("#")
            highlight_opacity = min(0.95, grid_opacity + 0.3)
            highlight_thickness = grid_thickness * 3
            half_t = highlight_thickness // 2

            if grid_highlight_windows:
                enable_parts = [f"between(t,{s:.3f},{e:.3f})" for s, e in grid_highlight_windows]
                enable_clause = f":enable='{'+'.join(enable_parts)}'"
            else:
                enable_clause = ""

            # `replace=1` overwrites pixels directly so the red dominates the
            # white base-grid line below it (without it, the alpha-blended
            # result reads as pinkish-white and is barely visible).
            filters.append(
                f"drawbox=x={v_x}-{half_t}:y=0:w={highlight_thickness}:h=ih"
                f":color=0x{highlight_hex}@{highlight_opacity:.2f}:t=fill"
                f":replace=1"
                f"{enable_clause}"
            )
            filters.append(
                f"drawbox=x=0:y={h_y}-{half_t}:w=iw:h={highlight_thickness}"
                f":color=0x{highlight_hex}@{highlight_opacity:.2f}:t=fill"
                f":replace=1"
                f"{enable_clause}"
            )

    # 6. Caption ASS (speech captions -- distinct from text overlay ASS)
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
