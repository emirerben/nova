"""Celery task for the transcript-flow `analyze` step.

Runs off the request path (clip download + ffprobe + optional Gemini summary are too
slow/heavy for a synchronous route — clip analysis lives in Celery everywhere else in
Kria). Probes total footage duration and produces a light footage summary, then writes
the result to the transcript store for the poll route to read.

Best-effort: any failure still writes a usable result (a default duration, no summary)
so the flow never dead-ends. `status` is "ready" unless the whole task hard-fails.
Ownership mismatch, quarantine, or a changed ownership epoch instead fails closed
without signing media paths or publishing a Redis result.
"""

from __future__ import annotations

import uuid

import structlog
from celery.exceptions import SoftTimeLimitExceeded

from app.database import sync_session
from app.models import ContentPlan, PlanItem
from app.services.content_plan_persona import (
    PlanPersonaOwnershipError,
    load_owned_plan_persona_sync,
)
from app.services.footage_summary import summarize_footage
from app.services.transcript_store import put_analyze
from app.worker import celery_app

log = structlog.get_logger()

# A creator with no probe-able clips still needs a target length; assume a short edit.
_DEFAULT_DURATION_S = 30.0


def _plan_epoch(plan: ContentPlan) -> int:
    return int(getattr(plan, "ownership_epoch", 0) or 0)


def _lock_owned_plan_item(
    db,  # noqa: ANN001
    *,
    plan_id: uuid.UUID,
    item_id: uuid.UUID,
    expected_epoch: int | None = None,
) -> tuple[ContentPlan, PlanItem] | None:
    """Lock and validate Plan -> Persona -> Item in global mutation order."""
    plan = db.get(ContentPlan, plan_id, with_for_update=True)
    if plan is None:
        return None
    load_owned_plan_persona_sync(db, plan, for_update=True)
    if expected_epoch is not None and _plan_epoch(plan) != expected_epoch:
        raise PlanPersonaOwnershipError(plan)
    item = db.get(PlanItem, item_id, with_for_update=True)
    if item is None or item.content_plan_id != plan.id:
        return None
    return plan, item


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
    # Preserve the legacy Celery signature, but never trust its media snapshot;
    # the live, fenced PlanItem below is the sole path source.
    _ = clip_gcs_paths
    try:
        iid = uuid.UUID(str(item_id))
    except (TypeError, ValueError):
        log.warning("transcript_analyze.bad_item_id", item_id=str(item_id))
        return

    with sync_session() as db:
        # The queued clip list is only a delivery hint. Reload the live item so
        # direct/legacy Celery calls cannot make us sign another tenant's path.
        item_ref = db.get(PlanItem, iid)
        if item_ref is None:
            return
        plan_id = item_ref.content_plan_id
        try:
            fenced = _lock_owned_plan_item(db, plan_id=plan_id, item_id=iid)
        except PlanPersonaOwnershipError:
            log.error("transcript_analyze.invalid_persona", item_id=item_id)
            return
        if fenced is None:
            return
        plan, item = fenced
        ownership_epoch = _plan_epoch(plan)
        paths = [p for p in (item.clip_gcs_paths or []) if isinstance(p, str) and p.strip()]

    result: dict = {"status": "ready", "duration_s": _DEFAULT_DURATION_S, "footage_summary": None}
    try:
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

    with sync_session() as db:
        try:
            fenced = _lock_owned_plan_item(
                db,
                plan_id=plan_id,
                item_id=iid,
                expected_epoch=ownership_epoch,
            )
        except PlanPersonaOwnershipError:
            log.warning("transcript_analyze.stale_owner", item_id=item_id)
            return
        if fenced is None:
            return
        # Keep the final ownership fence locked through this single bounded SET
        # so repair/quarantine and result publication have a linear order. The
        # transcript-store client has a 2s socket timeout, bounding lock hold.
        put_analyze(item_id, analyze_id, result)
