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
we ffprobe the file's side_data_list for "Display Matrix", and if a
non-zero orthogonal rotation is present we re-encode the local
intermediate with an explicit transpose filter and strip the metadata
flag. Real landscape sources (rotation = 0) are untouched — those are
reframe's intended case.

`-noautorotate` is critical: FFmpeg 7.x's decoder applies Display Matrix
rotation by default. Without `-noautorotate`, the decoder rotates AND our
transpose filter rotates → double-rotated garbage. With it, behavior is
identical on FFmpeg 5 / 6 / 7.

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


def detect_rotation(file_path: str) -> int:
    """Return Display-Matrix rotation in degrees, or 0 if none / non-orthogonal.

    ffprobe surfaces container rotation in the video stream's `side_data_list`
    as `{"side_data_type": "Display Matrix", "rotation": -90}`. The value is
    an integer; iPhones emit -90 for portrait-held landscape recording,
    Android emits 90, and 180 (upside-down) appears occasionally.

    Returns one of: -180, -90, 0, 90, 180.

    Non-orthogonal angles (e.g. 45) are normalized to 0 with a warning —
    the transpose filter only handles 90-degree multiples. Phones never
    emit these in practice; if we see one it's a malformed file and we
    fall through to reframe's normal handling.

    Raises OrientationError if ffprobe fails outright; that's a fatal
    signal (corrupt file, ffprobe missing).
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
        # No video stream is not our concern — orchestrator will fail
        # at the next step with a clearer error.
        return 0

    return _extract_rotation(video_stream)


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


def normalize_orientation(file_path: str) -> str:
    """If the file carries a Display-Matrix rotation flag, re-encode in
    place with explicit transpose + strip the flag. No-op otherwise.

    Returns the same `file_path` — the rewrite is atomic via a sibling
    `.tmp` file plus os.replace(). Callers don't need to track a new path.

    The output is an intermediate (downstream reframe re-encodes anyway),
    so libx264 ultrafast is the right quality budget. We intentionally do
    NOT route through `reframe._encoding_args` here — see the inline
    comment above the ffmpeg cmd for why.

    Raises OrientationError on ffmpeg failure. Fail-fast is intentional:
    silently shipping a yan-yatik clip after a normalize failure would be
    invisible to the user until they watched the final render.
    """
    # Lazy import: pipeline_trace pulls in SQLAlchemy + asyncpg which we
    # don't want at module-import time (orientation.py is imported by the
    # download path; the trace call only matters when a job_id is bound).
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    rotation = detect_rotation(file_path)
    if rotation == 0:
        # Record the no-op too. Surfaces in admin/jobs/{id} as evidence
        # that normalize ran and found nothing to do — answers the
        # "why didn't my upright-looking video get rotated" question
        # without the user having to grep Fly logs.
        record_pipeline_event(
            stage="orientation",
            event="skipped",
            data={"rotation": 0, "path": os.path.basename(file_path)},
        )
        return file_path

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
