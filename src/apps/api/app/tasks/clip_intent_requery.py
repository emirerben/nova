"""KRI-154: finish clip questions outside the chat request.

Each delivery owns one expiring asset/question claim. Only the answer cache is
written; a later chat turn resolves/grounds it and owns all session events.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import Any

import structlog
from pydantic import BaseModel, Field

from app.agents._model_client import default_client
from app.agents._runtime import RunContext
from app.agents.clip_question import ClipQuestionAgent, ClipQuestionOutput
from app.config import settings
from app.database import sync_session
from app.models import ContentPlan, PlanItem, PlanItemAsset
from app.services.clip_intent_resolution import (
    ANSWER_QUERIES_KEY,
    ANSWERS_KEY,
    DeferredVisionQuery,
    IntentClip,
    _failure_status_and_code,
    normalize_question,
    query_clip_vision,
    vision_query_marker,
    vision_query_pending,
)
from app.services.content_plan_persona import load_owned_plan_persona_sync
from app.services.pipeline_trace import pipeline_trace_for
from app.worker import celery_app

log = structlog.get_logger()
# Includes queue wait and three bounded attempts. Abandoned claims can be
# reclaimed by the next turn; an old delivery cannot overwrite a newer token.
QUERY_LEASE_S = 900


class VisionQueryJob(BaseModel):
    plan_id: uuid.UUID
    item_id: uuid.UUID
    asset_id: uuid.UUID
    user_id: uuid.UUID
    ownership_epoch: int
    gcs_path: str
    gcs_generation: str | None = None
    question: str = Field(min_length=1, max_length=200)
    token: str
    context: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return normalize_question(self.question)


def _owned_asset(db: Any, job: VisionQueryJob) -> PlanItemAsset | None:
    """Reload under the canonical Plan -> Persona -> Item -> Asset fence."""
    plan = db.get(ContentPlan, job.plan_id, with_for_update=True, populate_existing=True)
    if (
        plan is None
        or plan.user_id != job.user_id
        or int(plan.ownership_epoch or 0) != job.ownership_epoch
    ):
        return None
    load_owned_plan_persona_sync(db, plan, for_update=True)
    item = db.get(PlanItem, job.item_id, with_for_update=True, populate_existing=True)
    if item is None or item.content_plan_id != plan.id:
        return None
    asset = db.get(PlanItemAsset, job.asset_id, with_for_update=True, populate_existing=True)
    if (
        asset is None
        or asset.plan_item_id != item.id
        or asset.user_id != job.user_id
        or asset.status != "ready"
        or asset.kind != "video"
        or asset.deduplicated_to_asset_id is not None
        or asset.gcs_path != job.gcs_path
        or asset.gcs_generation != job.gcs_generation
    ):
        return None
    return asset


def _marker(asset: PlanItemAsset, key: str) -> dict[str, Any]:
    queries = (asset.analysis or {}).get(ANSWER_QUERIES_KEY)
    marker = queries.get(key) if isinstance(queries, dict) else None
    return marker if isinstance(marker, dict) else {}


def _set_marker(asset: PlanItemAsset, job: VisionQueryJob, status: str) -> None:
    analysis = dict(asset.analysis or {})
    queries = dict(analysis.get(ANSWER_QUERIES_KEY) or {})
    queries[job.key] = {
        "token": job.token,
        "status": status,
        "expires_at": time.time() + QUERY_LEASE_S,
        "generation": job.gcs_generation,
    }
    analysis[ANSWER_QUERIES_KEY] = queries
    asset.analysis = analysis


def _has_answer(asset: PlanItemAsset, key: str) -> bool:
    answers = (asset.analysis or {}).get(ANSWERS_KEY)
    answer = answers.get(key) if isinstance(answers, dict) else None
    return isinstance(answer, dict) and str(answer.get("generation") or "") == str(
        asset.gcs_generation or ""
    )


def _finish(
    job: VisionQueryJob,
    output: ClipQuestionOutput | None,
    *,
    status: str,
    error_code: str | None = None,
) -> None:
    with sync_session() as db:
        asset = _owned_asset(db, job)
        if asset is None or _marker(asset, job.key).get("token") != job.token:
            return
        _set_marker(asset, job, status)
        if error_code:
            asset.analysis[ANSWER_QUERIES_KEY][job.key]["error_code"] = error_code
        if output is not None and not _has_answer(asset, job.key):
            analysis = dict(asset.analysis or {})
            answers = dict(analysis.get(ANSWERS_KEY) or {})
            answers[job.key] = output.model_dump()
            if job.gcs_generation is not None:
                answers[job.key]["generation"] = job.gcs_generation
            analysis[ANSWERS_KEY] = answers
            asset.analysis = analysis
        db.commit()


def enqueue_clip_intent_requeries(
    *,
    plan_id: str,
    item_id: str,
    user_id: str,
    ownership_epoch: int,
    clips: list[IntentClip],
    queries: list[DeferredVisionQuery],
    context: RunContext,
) -> bool:
    """Claim and publish without a chat DB transaction. Failures ask the creator.

    Committing the claim before publishing lets a fast worker see it. A publish
    failure releases the claim, and a process crash in between expires naturally.
    Repeated turns share the claim instead of buying duplicate vision calls.
    """
    if not settings.clip_intents_enabled:
        return False
    clips_by_id = {clip.media_id: clip for clip in clips}
    pending = False
    for query in queries:
        clip = clips_by_id.get(query.media_id)
        if clip is None or not clip.asset_id or not clip.gcs_path or clip.kind != "video":
            continue
        job = None
        try:
            job = VisionQueryJob(
                plan_id=plan_id,
                item_id=item_id,
                asset_id=clip.asset_id,
                user_id=user_id,
                ownership_epoch=ownership_epoch,
                gcs_path=clip.gcs_path,
                gcs_generation=clip.generation,
                question=query.question,
                token=str(uuid.uuid4()),
                context=asdict(context),
            )
            with sync_session() as db:
                asset = _owned_asset(db, job)
                if asset is None:
                    continue
                if _has_answer(asset, job.key) or vision_query_pending(
                    asset.analysis, job.question, job.gcs_generation
                ):
                    pending = True
                    continue
                if vision_query_marker(asset.analysis, job.question, job.gcs_generation).get(
                    "error_code"
                ):
                    continue
                _set_marker(asset, job, "queued")
                db.commit()
            requery_clip_intent.apply_async(
                kwargs={"payload": job.model_dump(mode="json")},
                task_id=job.token,
                queue=settings.pool_asset_analysis_queue,
                expires=QUERY_LEASE_S,
                retry=False,
            )
            pending = True
        except Exception as exc:  # noqa: BLE001 - optional enrichment must not break chat
            log.warning(
                "clip_intents.enqueue_failed", media_id=query.media_id, error=str(exc)[:300]
            )
            if job is not None:
                try:
                    _finish(job, None, status="failed")
                except Exception:  # noqa: BLE001 - the lease still bounds recovery
                    log.warning("clip_intents.claim_release_failed", asset_id=str(job.asset_id))
    return pending


@celery_app.task(
    name="app.tasks.clip_intent_requery.requery_clip_intent",
    bind=True,
    max_retries=2,
    soft_time_limit=180,
    time_limit=240,
)
def requery_clip_intent(self: Any, payload: dict[str, Any]) -> None:
    if not settings.clip_intents_enabled:
        return
    job = VisionQueryJob.model_validate(payload)
    with sync_session() as db:
        asset = _owned_asset(db, job)
        if asset is None or _has_answer(asset, job.key):
            return
        marker = _marker(asset, job.key)
        if marker.get("token") != job.token:
            return
        if marker.get("status") == "running" and vision_query_pending(
            asset.analysis, job.question, job.gcs_generation
        ):
            return
        if marker.get("status") not in {"queued", "running"}:
            return
        _set_marker(asset, job, "running")
        db.commit()

    try:
        # No DB transaction or row lock spans downloads, upload, or model I/O.
        # There is no render job yet, so explicitly clear any inherited trace.
        with pipeline_trace_for(None):
            context = RunContext(**job.context)
            context.creator_id = str(job.user_id)
            output = query_clip_vision(
                IntentClip(
                    media_id=f"asset-{job.asset_id}",
                    asset_id=str(job.asset_id),
                    kind="video",
                    analysis=None,
                    gcs_path=job.gcs_path,
                    generation=job.gcs_generation,
                ),
                job.question,
                question_agent=ClipQuestionAgent(default_client()),
                run_context=context,
            )
        _finish(job, output, status="complete")
    except Exception as exc:  # noqa: BLE001
        _status, error_code = _failure_status_and_code(exc)
        if error_code == "vision_provider_error" and self.request.retries < self.max_retries:
            _finish(job, None, status="queued")
            raise self.retry(exc=exc, countdown=5) from exc
        # Preserve KRI-151's distinction: provider/media/budget failures are
        # technical states, never cached as a model's visual "unknown" answer.
        _finish(job, None, status="failed", error_code=error_code)
        log.warning("clip_intents.requery_failed", asset_id=str(job.asset_id), error=str(exc)[:300])
