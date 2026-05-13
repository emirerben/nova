"""Pre-emptive clip analysis — start Gemini upload+analyze the moment a clip
lands in GCS, before the user clicks Generate.

Why this exists: in the template upload flow the user spends 10-30 seconds
PUTting clips to GCS, then clicks "Generate video". The orchestrator then
downloads + Gemini-uploads + analyses each clip — another 30-60s on the
critical path. That second 30-60s is *redundant work for the same bytes*.

By kicking off prefetch the moment each PUT completes (client-side
`fetch().then(...)`) we move the Gemini stretch to run *during* the
remaining uploads. On submit, `_analyze_clips_with_cache` reads the warm
Redis entry and skips Gemini entirely. The win is bounded by how long the
user spent uploading: short uploads see no change, long uploads see most
of the Gemini phase disappear.

Architecture:
- Runs inside the FastAPI process via asyncio.create_task, not Celery.
  The API VM is otherwise idle while the worker chews on an orchestrate
  job; prefetch is mostly network-bound (GCS download + Gemini upload),
  so running it here doesn't fight FFmpeg for CPU.
- A module-level Semaphore caps concurrent in-flight prefetches at 4 so a
  burst from one user doesn't OOM the 512MB API VM.
- A module-level "in-flight" set dedupes within one API process. The
  Redis cache (`get_cached_meta` check before the Gemini round-trip)
  catches duplicates across processes.

Best-effort by design: any failure is logged and swallowed. The orchestrator
still does the upload+analyze itself when the cache is cold. A failed
prefetch makes the experience no worse than the pre-prefetch baseline.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from typing import Final

import structlog

log = structlog.get_logger()


# Cap concurrent in-flight prefetches per API process. 4 is well under the
# Gemini parallel cap of 16 used by the orchestrator (template_orchestrate.py)
# so a user's prefetch burst can't crowd out an actively-running orchestrate
# job's analyse phase.
_PREFETCH_CONCURRENCY: Final[int] = 4

# Lazy because Semaphore must be constructed inside the event loop.
_semaphore: asyncio.Semaphore | None = None

# In-flight dedup. Set entries are added on schedule and removed on completion
# (success OR failure). Doesn't survive process restart — that's OK, the
# Redis cache absorbs cross-process / cross-restart redundancy.
_in_flight: set[str] = set()
_in_flight_lock = asyncio.Lock()


# GCS path the upload endpoints generate: `{user_uuid}/batch-{batch_id}/clip_NNN.ext`
# We reject anything that doesn't match this exact shape so the endpoint
# can't be coerced into downloading arbitrary objects from our bucket.
_VALID_PATH_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"/batch-[0-9a-f]{12}"
    r"/clip_\d{3}\.[a-zA-Z0-9]{1,5}$"
)


def is_valid_prefetch_path(gcs_path: str) -> bool:
    """Whitelist: only paths produced by our presigned-URL flow are eligible.

    Drive imports use the same format. Standalone single-uploads (older
    `{user}/{job_id}/raw.mp4` shape) are out of scope — they're not in the
    template flow.
    """
    return bool(_VALID_PATH_RE.match(gcs_path))


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_PREFETCH_CONCURRENCY)
    return _semaphore


async def schedule_prefetch(gcs_path: str, filter_hint: str) -> bool:
    """Fire-and-forget: spawn the prefetch task. Returns False if duplicate.

    Caller's expected pattern:
        if not await schedule_prefetch(path, hint):
            log.info("prefetch_already_running", path=path)
        # caller returns 202 either way

    Why bool instead of throwing: the duplicate case is normal (rapid
    re-clicks, network retries) and not interesting to surface to the
    user. Logging at the call site is enough.
    """
    if not is_valid_prefetch_path(gcs_path):
        # Reject silently here too — the endpoint already 422'd before
        # calling us, this is defense in depth.
        log.warning("prefetch_rejected_invalid_path", gcs_path=gcs_path)
        return False

    async with _in_flight_lock:
        if gcs_path in _in_flight:
            return False
        _in_flight.add(gcs_path)

    # asyncio.create_task without awaiting — fire-and-forget. The task
    # owns its own try/except so an uncaught error can't take down the
    # event loop. Reference is dropped intentionally; the task keeps
    # itself alive until completion.
    asyncio.create_task(_run_prefetch(gcs_path, filter_hint))
    return True


async def _run_prefetch(gcs_path: str, filter_hint: str) -> None:
    """Do the prefetch work. Never raises — best-effort by design."""
    sem = _get_semaphore()
    try:
        async with sem:
            await _run_prefetch_locked(gcs_path, filter_hint)
    except Exception as exc:  # pragma: no cover — outer safety net
        log.warning(
            "prefetch_task_unexpected_failure",
            gcs_path=gcs_path,
            error=str(exc),
        )
    finally:
        async with _in_flight_lock:
            _in_flight.discard(gcs_path)


async def _run_prefetch_locked(gcs_path: str, filter_hint: str) -> None:
    """Body that runs while holding the concurrency semaphore."""
    # Imports inside the function so module import time stays cheap (this
    # service is reached on the warm path of /clips/prefetch-analyze).
    from app.pipeline.agents.gemini_analyzer import (  # noqa: PLC0415
        analyze_clip,
        gemini_upload_and_wait,
    )
    from app.pipeline.clip_cache import (  # noqa: PLC0415
        compute_clip_hash,
        get_cached_meta,
        set_cached_meta,
    )
    from app.storage import download_to_file  # noqa: PLC0415

    log.info("prefetch_start", gcs_path=gcs_path, filter_hint=filter_hint)

    with tempfile.TemporaryDirectory() as tmp:
        local = os.path.join(tmp, "clip.mp4")

        # ── Download ─────────────────────────────────────────────────────
        try:
            await asyncio.to_thread(download_to_file, gcs_path, local)
        except Exception as exc:
            # Most likely the user uploaded the URL but the PUT hadn't
            # actually finalized yet, or the file got deleted between
            # finalization and our download. Either way: orchestrator will
            # do the work later, this is best-effort.
            log.info("prefetch_download_failed", gcs_path=gcs_path, error=str(exc))
            return

        # ── Cache check (idempotency) ────────────────────────────────────
        clip_hash = await asyncio.to_thread(compute_clip_hash, local)
        if clip_hash is None:
            log.info("prefetch_hash_failed", gcs_path=gcs_path)
            return

        existing = await asyncio.to_thread(get_cached_meta, clip_hash, filter_hint)
        if existing is not None:
            log.info(
                "prefetch_skip_already_cached",
                gcs_path=gcs_path,
                clip_hash=clip_hash[:12],
            )
            return

        # ── Gemini upload + analyse ──────────────────────────────────────
        try:
            file_ref = await asyncio.to_thread(gemini_upload_and_wait, local)
        except Exception as exc:
            log.info("prefetch_gemini_upload_failed", gcs_path=gcs_path, error=str(exc))
            return

        try:
            meta = await asyncio.to_thread(
                analyze_clip, file_ref, None, None, filter_hint
            )
        except Exception as exc:
            log.info("prefetch_gemini_analyze_failed", gcs_path=gcs_path, error=str(exc))
            return

        # ── Cache write ──────────────────────────────────────────────────
        # set_cached_meta refuses to write degraded/failed metas; we don't
        # need to check that here.
        await asyncio.to_thread(set_cached_meta, clip_hash, filter_hint, meta)
        log.info(
            "prefetch_complete",
            gcs_path=gcs_path,
            clip_hash=clip_hash[:12],
            best_moments=len(meta.best_moments or []),
        )


def in_flight_count() -> int:
    """Diagnostic — number of prefetches currently scheduled or running."""
    return len(_in_flight)


def reset_state_for_tests() -> None:
    """Clear module state. Test-only — production code must not call this."""
    global _semaphore
    _semaphore = None
    _in_flight.clear()
