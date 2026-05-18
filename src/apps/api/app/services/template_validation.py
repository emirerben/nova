"""Shared template validation logic.

Extracted from template_jobs.py so both the public template-job endpoint
and the admin test-job endpoint can reuse the same checks.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import structlog
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import VideoTemplate
from app.pipeline.probe import probe_video
from app.storage import signed_get_url

log = structlog.get_logger()

# Upload-time guard for clips the deployed orientation pipeline cannot
# process within the 600s ffmpeg subprocess timeout.
#
# Empirical justification (measured on Fly worker `4d89590eb44587`,
# AMD EPYC shared-cpu-4x, FFmpeg 7.1.4):
#   - clip_008.MOV (HEVC Main 10 yuv420p10le, 1920x1080, 223s) timed out
#     at 300s and 600s in three separate prod jobs (5f897cca @ 15:16,
#     d1b9b9d8 @ 19:39, 71d22917 @ 20:26 on 2026-05-17).
#   - Uncontended Fly wall time for clip_008: 110-328s (10x shared-cpu
#     variance). Under realistic 8-thread + 2-permit-semaphore load,
#     wall time exceeds 600s.
#   - 8-bit clips of the same duration normalize ~3x faster (single-pass
#     pix-fmt decode, no 10-bit→8-bit bit-depth conversion) and fit the
#     budget comfortably.
#
# 60s is the longest 10-bit clip the deployed pipeline is empirically
# proven to fit inside its own timeout. Above that, the upload is
# refused at the API boundary with a remediation message instead of
# producing a 21-min processing failure for the user.
MAX_HDR_DURATION_S = 60


def _is_high_bit_pix_fmt(pix_fmt: str) -> bool:
    """True for ffprobe pix_fmts that represent 10-bit, 12-bit, or 16-bit
    sample depth. The cost cliff that motivates this guard is the bit-depth
    decode + tonemap path inside libx264 / libavcodec — not specifically
    the yuv420p10le 4:2:0 chroma layout. Matching the full family:

      - yuv420p10le / yuv420p10be — HEVC Main 10 (iPhone HLG, the original
        failure mode in jobs 5f897cca / d1b9b9d8 / 71d22917)
      - yuv422p10le / yuv444p10le — ProRes 422 HQ / 4444, DJI 10-bit
      - yuv420p12le / yuv422p12le / yuv444p12le — Cinema Camera output
      - p010le / p012le / p016le — NVENC / QSV / VAAPI HDR output
      - yuv420p16le et al. — exotic 16-bit pipelines

    Bypass attempt empirically anticipated: re-export the failing clip
    with ``-pix_fmt yuv422p10le`` to dodge an exact-match check. This
    substring test catches every 10-bit/12-bit/16-bit format the adversary
    can produce.
    """
    pf = pix_fmt.lower()
    if "10le" in pf or "10be" in pf:
        return True
    if "12le" in pf or "12be" in pf:
        return True
    if "16le" in pf or "16be" in pf:
        return True
    # NV12-style packed 10-bit / 12-bit / 16-bit output from hardware encoders.
    if pf.startswith(("p010", "p012", "p016")):
        return True
    return False


# Cap probe concurrency so a 20-clip upload doesn't fan out into 20
# simultaneous ffprobe-over-https calls against GCS. 16 picked empirically:
# a 15-clip batch against real prod GCS probed in ~22s at parallelism=8
# (two serialized batches of 8), but ffprobe-over-https is short-lived HTTP
# (range-read the moov atom, ~1-2s wall per clip), so the worker download
# pool's 8-cap is over-conservative here. 16 lets the 20-clip max upload
# size finish in approximately one round-trip cost (~2-3s) instead of
# three batches' worth. ffprobe subprocesses are ~10 MB resident each, so
# 16 concurrent uses ~160 MB on the 512 MB API container — comfortable.
_PREFLIGHT_PROBE_PARALLEL = 16


async def get_template_or_404(
    template_id: str,
    db: AsyncSession,
) -> VideoTemplate:
    """Fetch a template by ID or raise 404."""
    result = await db.execute(select(VideoTemplate).where(VideoTemplate.id == template_id))
    template = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Template not found",
        )
    return template


def require_ready(template: VideoTemplate) -> None:
    """Raise 409 if template analysis is not complete."""
    if template.analysis_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Template is still being analyzed (status: {template.analysis_status}). "
                "Try again in a few seconds."
            ),
        )


def validate_clip_count(template: VideoTemplate, n_clips: int) -> None:
    """Raise 422 if clip count is outside template bounds.

    Mixed-media templates (any slot with media_type=photo) require positional
    binding: clip count must equal slot count exactly.
    """
    slots = (template.recipe_cached or {}).get("slots") or []
    is_mixed_media = any(str(s.get("media_type", "video")) == "photo" for s in slots)

    if is_mixed_media:
        slot_count = len(slots)
        if n_clips != slot_count:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Template has photo slots and requires exactly "
                    f"{slot_count} clips in slot order, got {n_clips}."
                ),
            )
        return

    if n_clips < template.required_clips_min:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Template requires at least {template.required_clips_min} clips, got {n_clips}."
            ),
        )
    if n_clips > template.required_clips_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(f"Template allows at most {template.required_clips_max} clips, got {n_clips}."),
        )


async def validate_clips_processable(clip_gcs_paths: list[str]) -> None:
    """Reject uploads whose clips exceed the pipeline's empirical cost budget.

    The deployed orientation pipeline (`app/pipeline/orientation.py:normalize_orientation`)
    runs a transpose + 8-bit re-encode in a 600s subprocess timeout. A
    10-bit HEVC HDR clip longer than ~60s reliably blows that budget on
    Fly shared-cpu workers — see ``MAX_HDR_DURATION_S`` above for the
    empirical record. Rejecting at the API boundary saves the user a
    21-min worker hang.

    Probes each clip via a short-lived signed GCS URL. ffprobe range-
    requests only the moov atom, so a 400 MB clip is probed in ~1-2s
    rather than fully downloaded. Probes run in a thread pool capped at
    ``_PREFLIGHT_PROBE_PARALLEL`` so a 20-clip upload doesn't fan out
    into 20 simultaneous GCS reads.

    Raises HTTPException(400) with a structured ``code`` and per-clip
    diagnostics if any clip fails the guard.

    Tolerates probe failures (signed URL hiccup, ffprobe transient
    error, fork/IO errors under load): the orchestrator will probe the
    clip again on the worker and the worker-side path is responsible
    for surfacing that failure. Tolerating here keeps the guard from
    over-rejecting on infra blips, and matches the validator's stated
    contract under genuine load conditions (BlockingIOError, OSError,
    ENOMEM/EMFILE — all caught by the broad except below).

    Honors ``ORIENTATION_NORMALIZE_ENABLED=false`` — if ops have killed
    orientation normalization with the documented env-var safety valve,
    no clip should be rejected for failing a budget that won't be
    enforced anyway.
    """
    if not clip_gcs_paths:
        return

    # Kill-switch parity with `app/pipeline/orientation.py:normalize_orientation`.
    # When ops disable orientation normalize via env var, the cost cliff this
    # preflight defends against is no longer enforced — rejecting at the
    # boundary would just lock users out of a path that would now succeed.
    if os.getenv("ORIENTATION_NORMALIZE_ENABLED", "true").strip().lower() == "false":
        log.info("preflight_skipped_kill_switch_active", clip_count=len(clip_gcs_paths))
        return

    def _probe_one(idx_path: tuple[int, str]) -> tuple[int, str, float, str] | None:
        """Returns (idx, path, duration_s, pix_fmt) or None on probe failure."""
        idx, path = idx_path
        try:
            url = signed_get_url(path, expiration_minutes=5)
        except Exception as exc:  # GCS auth / network
            log.warning(
                "preflight_signed_url_failed",
                clip_index=idx,
                path=path,
                error=str(exc),
            )
            return None
        try:
            probe = probe_video(url)
        except Exception as exc:
            # Broad catch is deliberate. The documented contract is "tolerate
            # probe failures — the worker will re-probe and surface the real
            # error there." Under load conditions (BlockingIOError from
            # subprocess fork rate-limits, OSError on EMFILE, transient
            # google-auth refreshes), narrower catches let exceptions escape
            # asyncio.gather and turn the whole POST into a 500. The whole
            # guard is defense-in-depth — rejecting on infra blips is worse
            # than letting the worker handle it.
            log.warning(
                "preflight_probe_failed",
                clip_index=idx,
                path=path,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None
        return (idx, path, probe.duration_s, probe.pix_fmt)

    # `get_running_loop()` is the modern call (3.10+) — `get_event_loop()`
    # outside a running loop is deprecated and will start raising in 3.14.
    # We're inside an async function so the running loop is always present.
    loop = asyncio.get_running_loop()
    max_workers = min(len(clip_gcs_paths), _PREFLIGHT_PROBE_PARALLEL)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = await asyncio.gather(
            *[loop.run_in_executor(pool, _probe_one, (i, p)) for i, p in enumerate(clip_gcs_paths)]
        )

    offenders = [
        r
        for r in results
        if r is not None and r[2] > MAX_HDR_DURATION_S and _is_high_bit_pix_fmt(r[3])
    ]
    if not offenders:
        return

    # Pick the first offender for the headline error. The detail also
    # lists every offending clip so a 15-clip upload with two long-HDR
    # outliers gets both flagged in one response.
    first = offenders[0]
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": "clip_too_long_for_10bit",
            "clip_index": first[0],
            "duration_s": round(first[2], 1),
            "limit_s": MAX_HDR_DURATION_S,
            "pix_fmt": first[3],
            "message": (
                f"Clip {first[0] + 1} is {first[2]:.0f}s of 10-bit HDR footage. "
                f"Our current pipeline supports up to {MAX_HDR_DURATION_S}s for "
                f"10-bit HDR clips. Please trim this clip or re-export it as "
                f"8-bit (most phone share-sheets have an option for this)."
            ),
            "offenders": [
                {
                    "clip_index": idx,
                    "duration_s": round(dur, 1),
                    "pix_fmt": pf,
                }
                for (idx, _, dur, pf) in offenders
            ],
        },
    )


def validate_clip_total_duration(
    template: VideoTemplate,
    clip_durations: list[float] | None,
) -> None:
    """Raise 422 if the user's clips can't fill the template's audio length.

    For multi-video templates: sum(clip_durations) must be >= total_duration_s.
    For single-video templates: the lone clip must be at least as long.

    No-op when clip_durations is None or empty (legacy clients that don't
    measure duration on upload). The frontend reads durations via the
    HTML5 video element and sends them with the job; backend trusts that
    payload to keep job-create latency low.
    """
    if not clip_durations:
        return

    recipe = template.recipe_cached or {}
    required = float(recipe.get("total_duration_s") or 0.0)
    if required <= 0.0:
        return  # template has no duration metadata yet; can't enforce

    total = float(sum(clip_durations))
    # Tolerate sub-second rounding from browser duration reads.
    _TOLERANCE_S = 0.25
    if total + _TOLERANCE_S < required:
        short_by = required - total
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Your clips total {total:.1f}s but this template needs "
                f"{required:.1f}s of footage. Add {short_by:.1f}s more "
                f"(longer clip or another clip)."
            ),
        )
