"""Recoverable pre-planning analysis. This task never confirms or renders."""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents._runtime import (
    AiBudgetExceededError,
    ProviderOutcomeUnknownError,
    ProviderQuotaExceededError,
    RunContext,
)
from app.config import settings
from app.database import sync_session
from app.models import (
    ContentPlan,
    CreatorAgentEvent,
    CreatorAgentSession,
    CreatorPlanningAttempt,
    PlanItem,
)
from app.schemas.edit_proposal import MediaRef
from app.services.creator_clip_analysis import (
    analyze_clip_assignment,
    clip_analysis_ready,
    source_media_kind,
)
from app.services.creator_preparation import (
    ACTIVE_STATUSES,
    asset_query,
    owns_attempt,
    progress,
    publish_preparation,
    source_snapshot,
)
from app.services.pipeline_trace import pipeline_trace_for
from app.worker import celery_app

log = structlog.get_logger()
LEASE_SECONDS = 1830  # exceeds the hard task limit; a live provider call is never reclaimed
MAX_ATTEMPTS = 3


class PreparationStale(RuntimeError):
    pass


class PreparationPending(RuntimeError):
    pass


def _locked(db, identifier: uuid.UUID, *, token: str | None = None):
    ref = db.get(CreatorPlanningAttempt, identifier)
    if ref is None:
        return None
    item_ref = db.get(PlanItem, ref.plan_item_id)
    if item_ref is None:
        return None
    plan = db.get(
        ContentPlan, item_ref.content_plan_id, with_for_update=True, populate_existing=True
    )
    item = db.get(PlanItem, ref.plan_item_id, with_for_update=True, populate_existing=True)
    session = db.get(
        CreatorAgentSession, ref.session_id, with_for_update=True, populate_existing=True
    )
    attempt = db.get(
        CreatorPlanningAttempt, identifier, with_for_update=True, populate_existing=True
    )
    if not plan or not item or not session or not attempt:
        return None
    if token is not None and (attempt.status != "running" or attempt.lease_token != token):
        return None
    assets = list(db.execute(asset_query(item, attempt.creator_id)).scalars())
    sources = source_snapshot(item, assets)
    return attempt, session, plan, item, sources, assets


def _claim(identifier: uuid.UUID):
    with sync_session() as db:
        graph = _locked(db, identifier)
        if graph is None:
            return None
        attempt, session, plan, item, sources, _assets = graph
        if attempt.status not in ACTIVE_STATUSES:
            return None
        now = datetime.now(UTC)
        if attempt.status == "running" and attempt.lease_until and attempt.lease_until > now:
            return None
        if not owns_attempt(attempt, session, plan, item, sources):
            _supersede(db, attempt, session, plan)
            db.commit()
            return None
        if attempt.attempts >= MAX_ATTEMPTS:
            _record_failure(db, attempt, session, "preparation_interrupted", retryable=True)
            db.commit()
            return None
        attempt.status = "running"
        attempt.attempts += 1
        attempt.lease_token = str(uuid.uuid4())
        attempt.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        completed = sum(clip_analysis_ready(raw, kind=raw.get("kind")) for raw in sources)
        session.preparation = progress(attempt.id, "analyzing", completed, len(sources))
        payload = (
            attempt.lease_token,
            dict(attempt.inputs),
            sources,
            str(attempt.creator_id),
            str(attempt.session_id),
        )
        db.commit()
        return payload


def _checkpoint(identifier: uuid.UUID, token: str, analyzed: dict) -> None:
    with sync_session() as db:
        graph = _locked(db, identifier, token=token)
        if graph is None:
            raise PreparationStale()
        attempt, session, plan, item, sources, assets = graph
        if not owns_attempt(attempt, session, plan, item, sources):
            raise PreparationStale()
        if analyzed.get("asset_id"):
            # Reuse or refresh settled pool evidence only. Queued/running pool
            # jobs retain their own claim and are never overwritten.
            asset = next((a for a in assets if str(a.id) == analyzed["asset_id"]), None)
            if (
                asset is None
                or asset.status not in {"ready", "failed"}
                or str(asset.gcs_generation or "") != analyzed["generation"]
            ):
                raise PreparationStale()
            asset.analysis = analyzed["analysis"]
            asset.duration_s = analyzed.get("duration_s")
            asset.aspect = analyzed.get("aspect")
            asset.status = "ready"
            asset.error_code = None
            asset.error_detail = None
            asset.error_retryable = False
        else:
            from app.services.plan_item_media import (  # noqa: PLC0415
                current_detector_policy,
                mutate_plan_item_media,
            )
            from app.services.speech_cleanup_preflight import (
                mutation_current_analysis_sync,  # noqa: PLC0415
            )

            assignments = list(item.clip_assignments or [])
            if not assignments:
                assignments = [
                    dict(
                        media_id=f"legacy-clip-{i + 1}",
                        gcs_path=path,
                        kind=source_media_kind("", path),
                    )
                    for i, path in enumerate(item.clip_gcs_paths or [])
                ]
            merged = []
            found = False
            for index, raw in enumerate(assignments):
                entry = dict(raw)
                mid = entry.get("media_id") or f"clip-{index + 1}"
                entry["media_id"] = mid
                if (mid, entry.get("gcs_path")) == (analyzed["media_id"], analyzed["gcs_path"]):
                    registered = entry.get("storage_generation")
                    if registered and str(registered) != str(analyzed["generation"]):
                        raise PreparationStale()
                    for key in ("generation", "kind", "duration_s", "aspect", "analysis"):
                        if key in analyzed:
                            entry[key] = analyzed[key]
                    found = True
                merged.append(entry)
            if not found:
                raise PreparationStale()
            cleanup = mutation_current_analysis_sync(db, item.id, for_update=True)
            mutate_plan_item_media(
                item,
                detector_policy=current_detector_policy(),
                clip_assignments=merged,
                current_analysis=cleanup,
            )
        live_sources = source_snapshot(item, assets)
        completed = sum(clip_analysis_ready(raw, kind=raw.get("kind")) for raw in live_sources)
        session.preparation = progress(attempt.id, "analyzing", completed, len(live_sources))
        db.commit()


def _analyze_sources(
    identifier: uuid.UUID, token: str, sources: list[dict], ctx: RunContext
) -> None:
    pool = {
        raw["gcs_path"]: MediaRef(
            lane="asset",
            media_id=raw["media_id"],
            gcs_path=raw["gcs_path"],
            generation=raw["generation"],
            kind=raw["kind"],
            analysis=raw["analysis"],
            duration_s=raw.get("duration_s"),
            aspect=raw.get("aspect"),
        )
        for raw in sources
        if raw.get("asset_id") and clip_analysis_ready(raw, kind=raw.get("kind"))
    }
    candidates = []
    pool_pending = False
    for raw in sources:
        if raw.get("asset_id") and raw.get("asset_status") in {"uploaded", "queued", "analyzing"}:
            pool_pending = True
            continue
        candidates.append(raw)
    # Even ready source clips pass the helper's cheap immutable-generation
    # check; cached semantic records never trigger another paid analysis.
    executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="creator-prepare")
    pending = {}
    iterator = iter(candidates)
    success = False
    try:

        def submit_next():
            raw = next(iterator, None)
            if raw is None:
                return
            with sync_session() as db:
                graph = _locked(db, identifier, token=token)
                if graph is None or not owns_attempt(*graph[:4], graph[4]):
                    raise PreparationStale()
                allowed_prefixes = (
                    f"users/{ctx.creator_id}/",
                    f"plan/{getattr(graph[2], 'id', '')}/seed/",
                )
                if not str(raw.get("gcs_path") or "").startswith(allowed_prefixes):
                    raise PermissionError("source is not owned")
            clip_ctx = replace(
                ctx,
                request_id=f"{identifier}:{raw['media_id']}:{raw.get('storage_generation', '')}",
            )
            pending[
                executor.submit(
                    analyze_clip_assignment, raw, pool, run_context=clip_ctx, require_semantic=True
                )
            ] = raw

        for _ in range(3):
            submit_next()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                pending.pop(future)
                analyzed, _ref = future.result()
                if not clip_analysis_ready(analyzed, kind=analyzed.get("kind")):
                    raise RuntimeError("semantic analysis unavailable")
                _checkpoint(identifier, token, analyzed)
                submit_next()
        success = True
    finally:
        # Running Python threads cannot be killed. Their provider calls are
        # bounded and their writes stay fenced; never enqueue overlapping work.
        executor.shutdown(wait=True, cancel_futures=not success)
    if pool_pending:
        raise PreparationPending()


async def _resume(
    identifier: uuid.UUID, token: str, inputs: dict, creator_id: str, session_id: str
) -> None:
    from app.routes.creator_agent import _run_planning_turn  # noqa: PLC0415
    from app.services.creator_preparation import require_current_attempt  # noqa: PLC0415

    # asyncpg connections cannot be shared across Celery asyncio.run loops.
    engine = create_async_engine(settings.asyncpg_database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await require_current_attempt(db, str(identifier), token)
            attempt = await db.get(CreatorPlanningAttempt, identifier)
            session = await db.get(CreatorAgentSession, attempt.session_id)
            total = int((session.preparation or {}).get("total", 0))
            session.preparation = progress(identifier, "resolving", total, total)
            await db.commit()
            await _run_planning_turn(
                db,
                item_id=str(attempt.plan_item_id),
                user=SimpleNamespace(id=uuid.UUID(creator_id)),
                session_id=uuid.UUID(session_id),
                expected_revision=attempt.session_revision,
                preparation_attempt_id=str(identifier),
                preparation_token=token,
                **inputs,
            )
    finally:
        await engine.dispose()


def _record_failure(db, attempt, session, code: str, *, retryable: bool):
    attempt.status = "failed"
    attempt.error_code = code
    attempt.lease_until = None
    if (
        session.revision != attempt.session_revision
        or session.status not in {"planning", "revising"}
        or (session.preparation or {}).get("attempt_id") != str(attempt.id)
    ):
        return
    previous = session.preparation or {}
    state = progress(
        attempt.id,
        "failed",
        int(previous.get("completed", 0)),
        int(previous.get("total", 0)),
        error_code=code,
        retryable=retryable,
    )
    session.preparation = state
    session.status = "briefing"
    session.last_error = {"code": code}
    latest = db.execute(
        select(CreatorAgentEvent.sequence)
        .where(CreatorAgentEvent.session_id == session.id)
        .order_by(CreatorAgentEvent.sequence.desc())
        .limit(1)
    ).scalar_one_or_none()
    session.revision += 1
    db.add(
        CreatorAgentEvent(
            session_id=session.id,
            sequence=(latest if latest is not None else -1) + 1,
            event_type="assistant_error",
            role="assistant",
            revision=session.revision,
            payload={"message": state["message"], "code": code},
        )
    )


def _supersede(db, attempt, session, plan):
    # Only tell the unchanged owner of this exact pending request. Transfers,
    # cancellation and newer turns must never receive an old worker's event.
    if (
        plan.user_id == attempt.creator_id == session.creator_id
        and not getattr(plan, "ownership_quarantined_at", None)
        and int(plan.ownership_epoch or 0) == attempt.ownership_epoch == session.ownership_epoch
    ):
        _record_failure(db, attempt, session, "preparation_stale", retryable=True)
    attempt.status = "superseded"
    attempt.lease_until = None


def _fail(identifier: uuid.UUID, token: str, code: str, *, retryable: bool) -> None:
    with sync_session() as db:
        graph = _locked(db, identifier, token=token)
        if graph is None:
            return
        attempt, session, plan, item, sources, _assets = graph
        if owns_attempt(attempt, session, plan, item, sources):
            _record_failure(db, attempt, session, code, retryable=retryable)
        else:
            _supersede(db, attempt, session, plan)
        db.commit()


@celery_app.task(
    name="tasks.prepare_creator_clips",
    soft_time_limit=1740,
    time_limit=1800,
    acks_late=True,
    reject_on_worker_lost=True,
)
def prepare_creator_clips(attempt_id: str) -> None:
    identifier = uuid.UUID(attempt_id)
    claimed = _claim(identifier)
    if claimed is None:
        return
    token, inputs, sources, creator_id, session_id = claimed
    ctx = RunContext(
        creator_id=creator_id,
        creator_agent_session_id=session_id,
        **{
            k: inputs[k]
            for k in (
                "usage_purpose",
                "test_run_id",
                "estimated_max_cost_usd",
                "reservation_approved",
                "release_canary_id",
            )
            if k in inputs
        },
    )
    try:
        # Preparation precedes Job creation. Agent runs are attributed through
        # RunContext.creator_agent_session_id and durable progress through the attempt.
        with pipeline_trace_for(None):
            _analyze_sources(identifier, token, sources, ctx)
            asyncio.run(_resume(identifier, token, inputs, creator_id, session_id))
    except PreparationPending:
        # Pool tasks already own this work. Recheck later without spending
        # provider budget or consuming a crash-recovery attempt.
        with sync_session() as db:
            graph = _locked(db, identifier, token=token)
            if graph:
                attempt, session, *_ = graph
                if not owns_attempt(*graph[:4], graph[4]):
                    _supersede(db, attempt, session, graph[2])
                elif datetime.now(UTC) - attempt.created_at > timedelta(minutes=10):
                    _record_failure(db, attempt, session, "analysis_unavailable", retryable=True)
                else:
                    attempt.status = "queued"
                    attempt.attempts -= 1
                    attempt.lease_until = None
                db.commit()
    except ProviderQuotaExceededError as exc:
        log.warning(
            "creator_preparation.provider_denied",
            attempt_id=attempt_id,
            provider=exc.provider,
            reason=exc.reason,
        )
        _fail(identifier, token, "provider_quota_exceeded", retryable=True)
    except AiBudgetExceededError:
        _fail(identifier, token, "ai_budget_exhausted", retryable=True)
    except ProviderOutcomeUnknownError:
        _fail(identifier, token, "provider_outcome_unknown", retryable=False)
    except (PreparationStale, PermissionError, FileNotFoundError, ValueError):
        _fail(identifier, token, "media_unavailable", retryable=False)
    except Exception as exc:  # noqa: BLE001 — creators get stable safe errors
        log.warning(
            "creator_preparation.failed", attempt_id=attempt_id, error_type=type(exc).__name__
        )
        _fail(identifier, token, "analysis_unavailable", retryable=True)


@celery_app.task(name="tasks.reconcile_creator_preparations", soft_time_limit=45, time_limit=60)
def reconcile_creator_preparations() -> None:
    # Rollback stops NEW admissions in the API; existing durable work drains.
    now = datetime.now(UTC)
    with sync_session() as db:
        rows = list(
            db.execute(
                select(CreatorPlanningAttempt)
                .where(
                    CreatorPlanningAttempt.status.in_(ACTIVE_STATUSES),
                    or_(
                        CreatorPlanningAttempt.lease_until.is_(None),
                        CreatorPlanningAttempt.lease_until <= now,
                    ),
                    or_(
                        CreatorPlanningAttempt.last_dispatched_at.is_(None),
                        CreatorPlanningAttempt.last_dispatched_at <= now - timedelta(seconds=60),
                    ),
                )
                .order_by(CreatorPlanningAttempt.created_at)
                .limit(50)
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        ids = [str(row.id) for row in rows]
        for row in rows:
            row.last_dispatched_at = now
        db.commit()
    for identifier in ids:
        publish_preparation(identifier)
