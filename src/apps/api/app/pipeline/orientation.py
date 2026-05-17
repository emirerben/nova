"""[Stage 0.5] Display-Matrix rotation normalization for user uploads.

iPhone (and some Android) phones record video in physical landscape
(1920x1080) and write a Display Matrix side-data flag (e.g. rotation: -90)
into the MP4 container. QuickTime / TikTok / Instagram all read this flag
and present the video upright. Nova's downstream pipeline reads the raw
pixel dimensions and was getting confused:

  - `probe.py:_classify_aspect` labels a 1920x1080 file as "16:9", so
    `reframe.py` applies 16:9 face-tracked crop to landscape pixels →
    the user's portrait shot ships as a yan-yatik crop.
  - Gemini analyzes the raw uploaded file (see
    `_upload_clips_parallel` in `template_orchestrate.py`), so every
    `clip_metadata` field — detected_subject, composition,
    motion_direction, safe_zone_y_max — is computed against a sideways
    frame.

This module fixes both at the single download chokepoint. After download,
we ffprobe the file's side_data_list for "Display Matrix" alongside pixel
dimensions. Three cases:

  - No rotation flag (or non-orthogonal) → untouched.
  - Rotation flag + landscape pixels (the genuine iPhone-portrait recording
    case) → re-encode with an explicit transpose filter + strip flag.
  - Rotation flag + already-portrait pixels (iOS Photos exports etc. that
    carry a redundant flag on already-rotated bytes) → STRIP THE FLAG ONLY
    via codec-copy remux. Re-encoding here would double-rotate the clip
    (the regression that produced upside-down/sideways output in v0.4.27.0).

A kill switch is available: setting env var ``ORIENTATION_NORMALIZE_ENABLED``
to ``false`` and restarting workers makes ``normalize_orientation`` a no-op,
useful if a regression slips into prod.

Two input flags do the heavy lifting and must come BEFORE `-i`:

  - `-display_rotation 0` REPLACES the input AVStream's rotation matrix
    with identity. The muxer then writes an identity `tkhd` matrix on
    output, so ffprobe (and Gemini's decoder, and reframe's ffmpeg)
    sees no Display Matrix side_data. Available since FFmpeg 5.0.
    Bookworm worker ships FFmpeg 5.1.

  - `-noautorotate` belt-and-suspenders: prevents the decoder from
    applying ANY existing rotation before our transpose filter sees
    the frames. Without it, FFmpeg 7+ defaults would auto-rotate AND
    our transpose would rotate AGAIN → double-rotated garbage.

CI on Linux confirmed why `-metadata:s:v:0 rotate=0` alone (the legacy
form) and `-map_metadata -1` both fail: metadata and side_data are
separate FFmpeg constructs, and neither flag touches the Display
Matrix side_data entry. `-display_rotation 0` is the one that works.

CRITICAL: Never use shell=True. Always pass args as a list.
"""

from __future__ import annotations

import json
import os
import subprocess

import structlog

log = structlog.get_logger()

# ffprobe / ffmpeg timeouts. Probe is ~50ms; full re-encode of a 6s
# 1080p HEVC clip is ~3-10s on the worker — give 5min headroom for
# longer user clips at the 200MB upload cap.
_PROBE_TIMEOUT_S = 30
_NORMALIZE_TIMEOUT_S = 300

# Rotations we know how to handle. Phone cameras only emit orthogonal
# rotations (±90, 180). Non-orthogonal angles (45, etc.) never come from
# phones — if we see one, we leave the file alone and let reframe's
# normal pixel-dimension logic handle it.
_SUPPORTED_ROTATIONS = (-180, -90, 90, 180)


class OrientationError(Exception):
    """Raised when normalize_orientation cannot complete (ffmpeg failure,
    invalid rotation, etc). Bubbles to orchestrator's fail-fast path —
    silently shipping yan-yatik output is worse than failing the job."""


def detect_rotation_and_dims(file_path: str) -> tuple[int, int, int]:
    """Return ``(rotation_degrees, pixel_width, pixel_height)`` for a video file.

    One ffprobe call, three signals callers need together:

    - ``rotation_degrees`` — Display Matrix value, one of ``-180, -90, 0, 90, 180``.
      Non-orthogonal angles (e.g. 45) are normalized to 0 with a warning so we
      fall through to reframe's normal pixel-dim handling.
    - ``pixel_width`` / ``pixel_height`` — coded pixel dimensions (not display
      dimensions). For an iPhone portrait recording these are ``(1920, 1080)``;
      for an already-rotated portrait export they are ``(1080, 1920)``.

    The dim values exist so ``normalize_orientation`` can detect the stale-flag
    case: pixels already in target orientation (height > width) carrying a
    redundant rotation flag. Re-encoding such files double-rotates them.

    Raises ``OrientationError`` if ffprobe fails. Returns ``(0, 0, 0)`` when
    the input has no video stream — the orchestrator will fail with a clearer
    error at the next step.
    """
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        file_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OrientationError(
            f"ffprobe timed out after {_PROBE_TIMEOUT_S}s on {file_path}"
        ) from exc
    except FileNotFoundError as exc:
        raise OrientationError("ffprobe not found — is FFmpeg installed?") from exc

    if result.returncode != 0:
        raise OrientationError(f"ffprobe failed (rc={result.returncode}): {result.stderr[:300]}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OrientationError(f"ffprobe output is not valid JSON: {result.stdout[:200]}") from exc

    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        return (0, 0, 0)

    rotation = _extract_rotation(video_stream)
    try:
        width = int(video_stream.get("width") or 0)
        height = int(video_stream.get("height") or 0)
    except (TypeError, ValueError):
        width = height = 0
    return (rotation, width, height)


def detect_rotation(file_path: str) -> int:
    """Thin wrapper over :func:`detect_rotation_and_dims` for callers that only
    want the rotation flag. Kept for the existing test surface; new code should
    prefer ``detect_rotation_and_dims`` (single ffprobe call returning dims too).
    """
    rotation, _, _ = detect_rotation_and_dims(file_path)
    return rotation


def _extract_rotation(video_stream: dict) -> int:
    """Pull rotation degrees from a video stream's side_data_list.

    Exposed as a pure helper so tests can feed it canned ffprobe payloads
    without spawning subprocesses.
    """
    side_data_list = video_stream.get("side_data_list") or []
    for entry in side_data_list:
        if entry.get("side_data_type") != "Display Matrix":
            continue
        raw = entry.get("rotation")
        if raw is None:
            continue
        try:
            rotation = int(raw)
        except (TypeError, ValueError):
            continue
        if rotation in _SUPPORTED_ROTATIONS:
            return rotation
        # Phones don't emit non-orthogonal rotations. Log it and treat
        # as no-rotation so the file passes through to reframe's normal
        # 16:9 / 9:16 classification.
        log.warning(
            "orientation_non_orthogonal_rotation_ignored",
            rotation=rotation,
        )
        return 0
    return 0


def _transpose_filter_for(rotation: int) -> str:
    """Map a container rotation in degrees to an FFmpeg vf transpose chain.

    transpose=1 = 90° clockwise (top edge becomes right edge)
    transpose=2 = 90° counter-clockwise (top edge becomes left edge)
    180° = two CCW transposes (cheaper than hflip+vflip in this filter chain)

    The mapping reverses the container's "rotate by N degrees on
    playback" instruction: if the file says "rotate -90 on playback"
    (counter-clockwise), we apply that same -90 in pixels.
    """
    if rotation == -90 or rotation == 270:
        return "transpose=2"
    if rotation == 90 or rotation == -270:
        return "transpose=1"
    if rotation == 180 or rotation == -180:
        return "transpose=2,transpose=2"
    # Caller should have filtered to _SUPPORTED_ROTATIONS already.
    raise OrientationError(f"unsupported rotation {rotation}")


def _strip_rotation_flag_only(file_path: str) -> None:
    """Remux to clear the Display-Matrix rotation flag WITHOUT touching pixels.

    Used when ``normalize_orientation`` determines the pixel dims already
    match the target orientation and the rotation flag is stale. Applying a
    transpose in that case would double-rotate the clip (the bug PR #192
    introduced for iOS Photos exports).

    ``-c copy`` means codec-copy: ffmpeg remuxes the existing H.264/HEVC
    bitstream into a new container without decoding or re-encoding a single
    frame. Cost is roughly the file's I/O size, e.g. ~100ms for a 100MB
    intermediate vs. ~30s for a full re-encode on the worker.

    ``-display_rotation 0`` is the FFmpeg 5.0+ input flag that replaces the
    input AVStream's rotation matrix with identity. With ``-c copy`` the
    muxer writes the (now-identity) matrix to the output container, so
    ffprobe and every downstream consumer see the file as having no
    rotation metadata.

    Atomic via sibling ``.tmp`` file + ``os.replace`` — either the new file
    fully replaces the old, or the original is left untouched on failure.

    Raises ``OrientationError`` on ffmpeg failure.
    """
    tmp_path = f"{file_path}.flagstrip.tmp.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        # See `normalize_orientation` for the rationale behind both flags.
        # Short version: `-display_rotation 0` rewrites the input matrix to
        # identity; `-noautorotate` is belt-and-suspenders against decoder
        # auto-rotate behavior drift between FFmpeg versions.
        "-display_rotation",
        "0",
        "-noautorotate",
        "-i",
        file_path,
        # Codec copy: no pixel decode, no encode. Pure container remux.
        "-c",
        "copy",
        tmp_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_NORMALIZE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise OrientationError(
            f"ffmpeg flag-strip timed out after {_NORMALIZE_TIMEOUT_S}s on {file_path}"
        ) from exc

    if result.returncode != 0:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        stderr = result.stderr.decode("utf-8", errors="replace")[:500]
        raise OrientationError(f"ffmpeg flag-strip failed (rc={result.returncode}): {stderr}")

    os.replace(tmp_path, file_path)


def normalize_orientation(file_path: str) -> str:
    """Normalize a file's Display-Matrix rotation so downstream consumers see
    physical orientation matching the metadata.

    Two paths, picked based on rotation flag + pixel dims::

        rotation flag   pixel dims         action
        --------------  ----------------- --------------------------------
        0               any                no-op (event="skipped")
        ±90 / ±270      width  > height    full re-encode + transpose
                        height > width     strip flag, NO pixel rotation  ←
                        width == height    full re-encode (treat as landscape)
        ±180            width  > height    full re-encode + 180° transpose
                        height > width     strip flag, log warning        ←
                        width == height    full re-encode

    The marked ``←`` rows are why this function exists in its current form:
    iOS Photos and various export pipelines retain a rotation flag on files
    whose pixels are ALREADY in the rotated orientation. PR #192's original
    implementation always re-encoded with a transpose, which double-rotated
    those files and produced upside-down/sideways output for users.

    Both paths atomically replace ``file_path`` in place (.tmp + os.replace).

    Killable via env var ``ORIENTATION_NORMALIZE_ENABLED=false`` (default
    ``true``). Set to ``false`` and restart workers to make this function a
    no-op for ops emergencies — a regression here cannot survive past the
    next worker restart.

    Raises ``OrientationError`` on ffmpeg/ffprobe failure. Fail-fast is
    intentional: silently shipping a yan-yatık or upside-down clip would be
    invisible to the user until they watched the final render.
    """
    # Lazy import: pipeline_trace pulls in SQLAlchemy + asyncpg which we
    # don't want at module-import time (orientation.py is imported by the
    # download path; the trace call only matters when a job_id is bound).
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    # Kill switch. Ops can disable orientation normalization without a
    # redeploy by setting ORIENTATION_NORMALIZE_ENABLED=false on the worker
    # and restarting it. Used as a safety valve when a regression slips in.
    if os.getenv("ORIENTATION_NORMALIZE_ENABLED", "true").strip().lower() == "false":
        log.warning("orientation_disabled_by_env", path=file_path)
        record_pipeline_event(
            stage="orientation",
            event="disabled_by_env",
            data={"path": os.path.basename(file_path)},
        )
        return file_path

    rotation, width, height = detect_rotation_and_dims(file_path)

    if rotation == 0:
        # Record the no-op too. Surfaces in admin/jobs/{id} as evidence
        # that normalize ran and found nothing to do.
        record_pipeline_event(
            stage="orientation",
            event="skipped",
            data={
                "rotation": 0,
                "width": width,
                "height": height,
                "path": os.path.basename(file_path),
            },
        )
        return file_path

    # Stale-flag fast path: pixels are already in portrait orientation but
    # the container still carries a portrait rotation flag (iOS Photos
    # exports, share-sheet re-encodes, etc.). Re-encoding here would
    # rotate the already-upright pixels a second time → sideways/upside-down
    # output. Just strip the flag.
    #
    # Note: width == height (square) falls through to the re-encode path
    # below because a square video gives no signal about whether the
    # rotation flag is stale. iPhone Live Photo videos that record square
    # land here; trusting the flag is the safer default.
    if rotation in (-90, 90, -270, 270) and height > width:
        _strip_rotation_flag_only(file_path)
        log.info(
            "orientation_flag_stripped_no_rotation",
            path=file_path,
            rotation_flag=rotation,
            width=width,
            height=height,
        )
        record_pipeline_event(
            stage="orientation",
            event="flag_stripped_no_rotation",
            data={
                "rotation_flag": rotation,
                "width": width,
                "height": height,
                "path": os.path.basename(file_path),
            },
        )
        return file_path

    if rotation in (-180, 180) and height > width:
        # Already-portrait pixels with a 180° flag — ambiguous. Could be
        # an intentional upside-down recording (rare) or a stale flag
        # (common for re-exported clips). Default to "stale" and log so
        # we catch genuine cases via the admin job-debug view.
        log.warning(
            "orientation_180_on_portrait_pixels_stripping_flag",
            path=file_path,
            width=width,
            height=height,
        )
        _strip_rotation_flag_only(file_path)
        record_pipeline_event(
            stage="orientation",
            event="flag_stripped_no_rotation_180",
            data={
                "rotation_flag": rotation,
                "width": width,
                "height": height,
                "path": os.path.basename(file_path),
            },
        )
        return file_path

    # Landscape (or square) pixels with a rotation flag — the genuine
    # iPhone-portrait-recording case PR #192 was shipped to fix. Full
    # re-encode with transpose.
    transpose_chain = _transpose_filter_for(rotation)
    tmp_path = f"{file_path}.norot.tmp.mp4"

    # Intermediate-quality libx264 args. Deliberately NOT calling
    # `reframe._encoding_args` — that helper bakes in `-s 1080x1920`,
    # `-r 30`, `BODY_SLOT_AUDIO_OUT_ARGS`, and bt709 colorspace re-tagging
    # that are correct for the final reframe output but wrong here. The
    # normalize step is an in-place dimension-preserving rewrite; reframe
    # re-encodes the result to final spec downstream.
    #
    # Preset rationale: ultrafast is policy-compliant for intermediate
    # re-encodes (see tests/test_encoder_policy.py docstring). The output
    # is re-encoded by reframe, so the macroblocking ultrafast causes on
    # smooth gradients gets resampled away in the final pass.
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        # `-display_rotation 0` REPLACES the input AVStream's rotation
        # matrix with identity. With this, the muxer writes an identity
        # `tkhd` matrix on output → ffprobe sees no Display Matrix
        # side_data → downstream consumers (probe.py, Gemini upload,
        # reframe ffmpeg) all agree the file has no rotation. Must come
        # BEFORE `-i`. Available since FFmpeg 5.0 (Bookworm has 7.x).
        #
        # CRITICAL — why not the earlier approaches:
        #   - `-metadata:s:v:0 rotate=0` only clears the LEGACY `rotate`
        #     tag, not the Display Matrix side_data entry. macOS
        #     Homebrew 8.1 happened to strip it via the transpose
        #     filter's side-effect, hiding this gap locally.
        #   - `-map_metadata -1` discards metadata mapping but does NOT
        #     touch side_data — those are separate FFmpeg constructs.
        #     CI on Linux FFmpeg 7.x confirmed: pixels rotated correctly
        #     via transpose but ffprobe still reported `Display Matrix
        #     rotation=-90` on the output. That would have caused
        #     Gemini's autorotating decoder AND `reframe.py`'s ffmpeg
        #     (no `-noautorotate`) to rotate the already-portrait pixels
        #     a SECOND time → original yan-yatık bug returns.
        #
        # `-noautorotate` is kept as belt-and-suspenders: with
        # `-display_rotation 0` the matrix is already 0 so the decoder
        # has nothing to apply, but the redundancy costs nothing and
        # locks behavior across any future FFmpeg version drift.
        "-display_rotation",
        "0",
        "-noautorotate",
        "-i",
        file_path,
        "-vf",
        transpose_chain,
        # Video: minimal H.264 intermediate. yuv420p is the universal
        # decoder-friendly format; high profile matches what reframe
        # produces. CRF 18 is visually lossless for a single intermediate
        # hop. No -bf 0 / no -x264-params: reframe re-encodes anyway and
        # closed-GOP concerns only matter at the final concat boundary.
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-preset",
        "ultrafast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        # Audio is unchanged — codec-copy avoids a wasted aac re-encode.
        "-c:a",
        "copy",
        tmp_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_NORMALIZE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # Best-effort cleanup; missing tmp file is fine.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise OrientationError(
            f"ffmpeg normalize timed out after {_NORMALIZE_TIMEOUT_S}s on {file_path}"
        ) from exc

    if result.returncode != 0:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        stderr = result.stderr.decode("utf-8", errors="replace")[:500]
        raise OrientationError(f"ffmpeg normalize failed (rc={result.returncode}): {stderr}")

    # Atomic swap — on POSIX os.replace is rename(2). Either the new file
    # is fully written when we replace, or we leave the original untouched.
    os.replace(tmp_path, file_path)

    log.info(
        "orientation_normalized",
        path=file_path,
        rotation_applied=rotation,
        transpose=transpose_chain,
    )
    record_pipeline_event(
        stage="orientation",
        event="normalized",
        data={
            "rotation_applied": rotation,
            "transpose": transpose_chain,
            "path": os.path.basename(file_path),
        },
    )
    return file_path
