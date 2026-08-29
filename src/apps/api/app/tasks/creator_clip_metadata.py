"""Persist creator clip identities and durations outside web requests.

The attach route owns path validation. This worker nevertheless re-establishes
ownership before signing an object because JSONB is not a security boundary.
External probes run without database locks; results are merged into the latest
assignment list by the exact ``(media_id, gcs_path)`` pair.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from app.config import settings
from app.database import sync_session
from app.models import ContentPlan, PlanItem
from app.services.plan_clips import ensure_clip_media_ids
from app.worker import celery_app

log = structlog.get_logger()
_MAX_CLIPS_PER_ITEM = 50
# Local dev consumes the configured default (``celery``); production can keep
# metadata probes isolated by setting POOL_ASSET_ANALYSIS_QUEUE=autoplace-jobs.
CREATOR_CLIP_METADATA_QUEUE = settings.pool_asset_analysis_queue
_PER_CLIP_WORST_CASE_S = 20  # generation read + ffprobe + generation recheck
_TASK_SOFT_LIMIT_S = _MAX_CLIPS_PER_ITEM * _PER_CLIP_WORST_CASE_S + 120
_TASK_HARD_LIMIT_S = _TASK_SOFT_LIMIT_S + 60
_PROBE_LEASE_S = _TASK_HARD_LIMIT_S + 60
_PROBE_FAILURE_BACKOFF_S = 60


def _coerce_epoch(value: object) -> int | None:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _positive_duration_s(value: object) -> float | None:
    try:
        duration_s = float(value or 0)
    except (TypeError, ValueError):
        return None
    if duration_s <= 0 or not math.isfinite(duration_s):
        return None
    return duration_s


def _allowed_clip_path(path: str, *, plan: ContentPlan, item: PlanItem) -> bool:
    owner_id = str(plan.user_id)
    return (
        path.startswith(f"users/{owner_id}/plan/{item.id}/")
        or path.startswith(f"users/{owner_id}/plan-pool/{plan.id}/")
        or path.startswith(f"users/{owner_id}/plan/{plan.id}/seed/")
    )


def _probe_clip_duration_s(gcs_path: str) -> float | None:
    from app.pipeline.intro_voiceover_mix import _probe_duration  # noqa: PLC0415
    from app.storage import signed_get_url  # noqa: PLC0415

    return _positive_duration_s(_probe_duration(signed_get_url(gcs_path)))


def _object_generation(gcs_path: str) -> str:
    from app.storage import object_metadata_once  # noqa: PLC0415

    return str(object_metadata_once(gcs_path, timeout_s=5).generation)


def _marker_is_fresh(
    entry: dict[str, object],
    *,
    generation: str,
    now: datetime,
) -> bool:
    if str(entry.get("duration_probe_generation") or "") != generation:
        return False
    status = str(entry.get("duration_probe_status") or "")
    ttl_s = _PROBE_LEASE_S if status == "probing" else _PROBE_FAILURE_BACKOFF_S
    if status not in {"probing", "failed"}:
        return False
    try:
        attempted_at = datetime.fromisoformat(str(entry.get("duration_probe_attempted_at") or ""))
    except ValueError:
        return False
    if attempted_at.tzinfo is None:
        attempted_at = attempted_at.replace(tzinfo=UTC)
    return 0 <= (now - attempted_at).total_seconds() < ttl_s


def _lock_owned_item(
    session: Any,
    *,
    item_id: uuid.UUID,
    expected_ownership_epoch: int,
) -> tuple[ContentPlan, PlanItem] | None:
    """Acquire the canonical Plan -> PlanItem fence for a live delivery."""

    item_ref = session.get(PlanItem, item_id)
    if item_ref is None:
        return None
    plan = session.get(ContentPlan, item_ref.content_plan_id, with_for_update=True)
    if (
        plan is None
        or int(getattr(plan, "ownership_epoch", 0) or 0) != expected_ownership_epoch
        or getattr(plan, "ownership_quarantined_at", None) is not None
    ):
        return None
    item = session.get(PlanItem, item_id, with_for_update=True, populate_existing=True)
    if item is None or item.content_plan_id != plan.id:
        return None
    return plan, item


def _run(plan_item_id: str, *, expected_ownership_epoch: int) -> None:
    try:
        item_id = uuid.UUID(str(plan_item_id))
    except (TypeError, ValueError):
        log.warning("creator_clip_metadata.bad_item_id", plan_item_id=str(plan_item_id))
        return

    # Persist stable IDs and snapshot candidates, then release every DB lock
    # before storage I/O. Existing durations are included so an overwritten
    # object generation can never inherit stale metadata.
    with sync_session() as session:
        owned = _lock_owned_item(
            session,
            item_id=item_id,
            expected_ownership_epoch=expected_ownership_epoch,
        )
        if owned is None:
            return
        plan, item = owned
        ids_changed = ensure_clip_media_ids(item)
        candidates: list[tuple[str, str]] = []
        for raw in (item.clip_assignments or [])[:_MAX_CLIPS_PER_ITEM]:
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("gcs_path") or "")
            media_id = str(raw.get("media_id") or "")
            if path and media_id and _allowed_clip_path(path, plan=plan, item=item):
                candidates.append((media_id, path))
        if ids_changed:
            session.commit()
        else:
            session.rollback()

    if not candidates:
        return

    generations: dict[tuple[str, str], str] = {}
    for media_id, path in candidates:
        try:
            generations[(media_id, path)] = _object_generation(path)
        except Exception as exc:  # noqa: BLE001 - missing objects remain unavailable
            log.warning(
                "creator_clip_metadata.metadata_failed",
                plan_item_id=plan_item_id,
                path=path[:160],
                error=str(exc)[:240],
            )

    if not generations:
        return

    # Claim each exact object generation under the item lock. Duplicate tasks
    # may perform cheap metadata reads, but only one can claim/probe a clip.
    now = datetime.now(UTC)
    claimed: list[tuple[str, str, str]] = []
    with sync_session() as session:
        owned = _lock_owned_item(
            session,
            item_id=item_id,
            expected_ownership_epoch=expected_ownership_epoch,
        )
        if owned is None:
            return
        plan, item = owned
        changed = False
        merged: list[object] = []
        live_paths = set(item.clip_gcs_paths or [])
        for raw in item.clip_assignments or []:
            if not isinstance(raw, dict):
                merged.append(raw)
                continue
            entry = dict(raw)
            path = str(entry.get("gcs_path") or "")
            media_id = str(entry.get("media_id") or "")
            generation = generations.get((media_id, path))
            if (
                generation is not None
                and path in live_paths
                and _allowed_clip_path(path, plan=plan, item=item)
            ):
                if (
                    _positive_duration_s(entry.get("duration_s")) is not None
                    and str(entry.get("duration_probe_generation") or "") == generation
                ):
                    merged.append(entry)
                    continue
                if not _marker_is_fresh(entry, generation=generation, now=now):
                    entry.pop("duration_s", None)
                    entry["duration_probe_status"] = "probing"
                    entry["duration_probe_generation"] = generation
                    entry["duration_probe_attempted_at"] = now.isoformat()
                    claimed.append((media_id, path, generation))
                    changed = True
            merged.append(entry)
        if changed:
            item.clip_assignments = merged
            session.commit()
        else:
            session.rollback()

    if not claimed:
        return

    # Sequential ffprobe keeps signed-request/process amplification bounded by
    # the dedicated analysis worker's own concurrency.
    results: dict[tuple[str, str, str], float | None] = {}
    for media_id, path, generation in claimed:
        try:
            duration_s = _probe_clip_duration_s(path)
        except Exception as exc:  # noqa: BLE001 - corrupt media enters backoff
            log.warning(
                "creator_clip_metadata.probe_failed",
                plan_item_id=plan_item_id,
                path=path[:160],
                error=str(exc)[:240],
            )
            duration_s = None
        results[(media_id, path, generation)] = (
            round(duration_s, 3) if duration_s is not None else None
        )

    # Recheck generation after the probe. A same-path overwrite during ffprobe
    # is discarded and becomes eligible on the next delivery.
    stable_generations: set[tuple[str, str, str]] = set()
    for media_id, path, generation in claimed:
        try:
            if _object_generation(path) == generation:
                stable_generations.add((media_id, path, generation))
        except Exception:  # noqa: BLE001 - fail closed on unverifiable identity
            continue

    finished_at = datetime.now(UTC).isoformat()
    with sync_session() as session:
        owned = _lock_owned_item(
            session,
            item_id=item_id,
            expected_ownership_epoch=expected_ownership_epoch,
        )
        if owned is None:
            return
        plan, item = owned
        changed = False
        merged = []
        live_paths = set(item.clip_gcs_paths or [])
        for raw in item.clip_assignments or []:
            if not isinstance(raw, dict):
                merged.append(raw)
                continue
            entry = dict(raw)
            path = str(entry.get("gcs_path") or "")
            media_id = str(entry.get("media_id") or "")
            generation = str(entry.get("duration_probe_generation") or "")
            key = (media_id, path, generation)
            if (
                key in results
                and path in live_paths
                and _allowed_clip_path(path, plan=plan, item=item)
                and entry.get("duration_probe_status") == "probing"
            ):
                duration_s = results[key] if key in stable_generations else None
                if duration_s is not None:
                    entry["duration_s"] = duration_s
                    entry["duration_probe_status"] = "ready"
                else:
                    entry.pop("duration_s", None)
                    entry["duration_probe_status"] = "failed"
                entry["duration_probe_attempted_at"] = finished_at
                changed = True
            merged.append(entry)
        if changed:
            item.clip_assignments = merged
            session.commit()
        else:
            session.rollback()


@celery_app.task(
    name="app.tasks.creator_clip_metadata.analyze_creator_clip_metadata",
    queue=CREATOR_CLIP_METADATA_QUEUE,
    max_retries=0,
    soft_time_limit=_TASK_SOFT_LIMIT_S,
    time_limit=_TASK_HARD_LIMIT_S,
)
def analyze_creator_clip_metadata(
    plan_item_id: str,
    expected_ownership_epoch: int | None = None,
) -> None:
    """Best-effort metadata extraction for the item's current owned clips."""

    epoch = _coerce_epoch(expected_ownership_epoch)
    if epoch is None:
        log.error("creator_clip_metadata.missing_dispatch_epoch", plan_item_id=plan_item_id)
        return
    try:
        _run(plan_item_id, expected_ownership_epoch=epoch)
    except Exception as exc:  # noqa: BLE001 - planning fails closed until a retry succeeds
        log.warning(
            "creator_clip_metadata.failed",
            plan_item_id=plan_item_id,
            error=str(exc)[:400],
        )
