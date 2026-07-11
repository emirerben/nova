"""Celery task for the transcript-flow `analyze` step.

Runs off the request path (clip download + ffprobe + optional Gemini summary are too
slow/heavy for a synchronous route — clip analysis lives in Celery everywhere else in
Kria). Probes total footage duration and produces a light footage summary, then writes
the result to the transcript store for the poll route to read.

Best-effort: any failure still writes a usable result (a default duration, no summary)
so the flow never dead-ends. `status` is "ready" unless the whole task hard-fails.
"""

from __future__ import annotations

import structlog
from celery.exceptions import SoftTimeLimitExceeded

from app.services.footage_summary import summarize_footage
from app.services.transcript_store import put_analyze
from app.worker import celery_app

log = structlog.get_logger()

# A creator with no probe-able clips still needs a target length; assume a short edit.
_DEFAULT_DURATION_S = 30.0


def _probe_total_duration(clip_gcs_paths: list[str]) -> float:
    """Sum ffprobe durations across clips via signed stream-probe URLs (no download)."""
    from app.pipeline.intro_voiceover_mix import _probe_duration  # noqa: PLC0415
    from app.storage import signed_get_url  # noqa: PLC0415

    total = 0.0
    for path in clip_gcs_paths:
        try:
            total += _probe_duration(signed_get_url(path))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "transcript_analyze.probe_failed", path=str(path)[:120], error=str(exc)[:200]
            )
    return round(total, 2)


@celery_app.task(name="transcript.analyze", soft_time_limit=120, time_limit=180)
def analyze_transcript_footage(analyze_id: str, clip_gcs_paths: list[str], item_id: str) -> None:
    result: dict = {"status": "ready", "duration_s": _DEFAULT_DURATION_S, "footage_summary": None}
    try:
        paths = [p for p in (clip_gcs_paths or []) if isinstance(p, str) and p.strip()]
        duration = _probe_total_duration(paths)
        if duration > 0:
            result["duration_s"] = duration
        result["footage_summary"] = summarize_footage(paths)
    except SoftTimeLimitExceeded:
        # Timed out — hand back a usable default so the flow proceeds brief-only.
        result = {"status": "ready", "duration_s": _DEFAULT_DURATION_S, "footage_summary": None}
        log.warning("transcript_analyze.soft_timeout", analyze_id=analyze_id)
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        log.warning("transcript_analyze.failed", analyze_id=analyze_id, error=str(exc)[:300])
    finally:
        put_analyze(item_id, analyze_id, result)
