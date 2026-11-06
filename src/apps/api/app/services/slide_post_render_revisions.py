"""Durable catch-up for saved versions of keyed SlidePosts; never repeat first compute."""

from __future__ import annotations

import copy
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from functools import wraps

import structlog
from sqlalchemy import or_, select

from app.config import settings
from app.database import sync_session
from app.kria.media_sources import is_device_render_job
from app.models import Job, PlanItem, SlidePostGenerationReceipt, SlidePostRenderRevision
from app.schemas.slide_post import parse_slide_post
from app.services import slide_post_generation_delivery as first
from app.services.cloud_render_policy import cloud_render_mutation_block_reason
from app.services.job_status import PLAN_ITEM_JOB_FAILED
from app.services.slide_post_generation_receipts import ReceiptConflict, _digest, capture_inputs

log = structlog.get_logger()
TERMINAL = {"completed", "failed", "superseded", "blocked"}


def _render_block_reason(job, *, variant=None, native_client=False):
    if is_device_render_job(job) or (
        isinstance(variant, dict) and variant.get("render_destination") in {"device", "phone"}
    ):
        return "device_render_owned"
    return cloud_render_mutation_block_reason(job, variant=variant, native_client=native_client)


def source_identity(asset: dict) -> str:
    return (
        asset.get("content_fingerprint")
        or hashlib.sha256(
            f"{asset.get('gcs_path')}@{asset.get('gcs_generation')}".encode()
        ).hexdigest()
    )


def normalized_prefix(job_id: str, *, strict: bool) -> str:
    base = f"generative-jobs/{job_id}/slides/normalized"
    return f"{base}/complete-v1" if strict else base


def artifact_prefix(job_id: str, generation: str | None) -> str:
    base = f"generative-jobs/{job_id}/slides"
    return f"{base}/renders/{generation}" if generation else base


def stage_saved_revision(db, item: PlanItem, *, native_client: bool = False) -> bool:
    """Called inside the draft Save transaction; blocked rendering never rejects a Save."""
    parent = db.scalar(
        select(SlidePostGenerationReceipt)
        .where(SlidePostGenerationReceipt.job_id == item.current_job_id)
        .with_for_update()
    )
    if parent is None:
        return False
    draft = parse_slide_post(item.slide_post)
    if draft is None or draft.version <= parent.draft_version:
        return True
    existing = db.scalar(
        select(SlidePostRenderRevision).where(
            SlidePostRenderRevision.parent_request_id == parent.client_request_id,
            SlidePostRenderRevision.draft_version == draft.version,
        )
    )
    if existing:
        return True
    error = None
    try:
        snapshot, assets = capture_inputs(db, item, draft, parent.owner_id)
    except ReceiptConflict as exc:
        snapshot, assets, error = draft.model_dump(mode="json"), {}, str(exc)
    job = db.get(Job, parent.job_id)
    blocked = _render_block_reason(job, native_client=native_client) if job else None
    state = (
        "failed"
        if error
        or parent.execution_state == "failed"
        or not job
        or job.status in PLAN_ITEM_JOB_FAILED
        else "blocked"
        if blocked
        else "pending"
    )
    db.add(
        SlidePostRenderRevision(
            id=uuid.uuid4(),
            parent_request_id=parent.client_request_id,
            job_id=parent.job_id,
            item_id=item.id,
            owner_id=parent.owner_id,
            ownership_epoch=parent.ownership_epoch,
            draft_version=draft.version,
            draft_snapshot=snapshot,
            asset_snapshot=assets,
            snapshot_digest=_digest({"draft": snapshot, "assets": assets}),
            native_client=native_client,
            state=state,
            error=error or blocked or ("first_generation_failed" if state == "failed" else None),
        )
    )
    return True


def _locked(db, identifier):
    ref = db.get(SlidePostRenderRevision, identifier)
    if ref is None:
        return None
    graph = first.lock_graph(db, ref.job_id)
    revision = db.get(
        SlidePostRenderRevision, identifier, with_for_update=True, populate_existing=True
    )
    if revision is None or revision.state in TERMINAL:
        return None
    if graph is None:
        revision.state, revision.error = "superseded", "target_unavailable"
        db.commit()
        return None
    item, job, parent = graph
    if (
        revision.parent_request_id != parent.client_request_id
        or revision.job_id != job.id
        or revision.owner_id != parent.owner_id
        or revision.item_id != item.id
        or revision.ownership_epoch != parent.ownership_epoch
    ):
        revision.state, revision.error = "failed", "revision_identity_mismatch"
        db.commit()
        return None
    if parent.execution_state == "failed" or job.status in PLAN_ITEM_JOB_FAILED:
        revision.state, revision.error = "failed", "first_generation_failed"
        db.commit()
        return None
    if (item.slide_post or {}).get("version") != revision.draft_version:
        revision.state, revision.error = "superseded", "newer_saved_draft"
        db.commit()
        return None
    return item, job, parent, revision


def _variant(job):
    return next(
        (
            v
            for v in (job.assembly_plan or {}).get("variants", [])
            if v.get("variant_id") == "slides"
        ),
        None,
    )


def _fail(db, graph, reason):
    _item, job, _parent, revision = graph
    revision.state, revision.error = "failed", reason
    variant = _variant(job)
    if variant and variant.get("render_generation_id") == str(revision.id):
        changed = {**variant, "render_status": "failed", "error": reason}
        job.assembly_plan = {
            **job.assembly_plan,
            "variants": [
                changed if v.get("variant_id") == "slides" else v
                for v in job.assembly_plan["variants"]
            ],
        }
    db.commit()


def claim_revision(identifier, job_id=None) -> bool:
    identifier = uuid.UUID(str(identifier))
    with sync_session() as db:
        graph = _locked(db, identifier)
        if graph is None:
            return False
        _item, job, parent, revision = graph
        variant = _variant(job)
        if (
            (job_id is not None and str(job.id) != str(job_id))
            or parent.execution_state != "completed"
            or revision.state != "pending"
        ):
            return False
        if not variant or variant.get("render_generation_id") != str(identifier):
            return False
        if _render_block_reason(job, variant=variant, native_client=revision.native_client):
            _fail(db, graph, "render_policy_blocked")
            return False
        revision.state, revision.claimed_at, revision.next_observe_at = (
            "running",
            datetime.now(UTC),
            None,
        )
        db.commit()
        return True


def revision_snapshot(db, job: Job, identifier):
    row = db.get(SlidePostRenderRevision, uuid.UUID(str(identifier)))
    if (
        row is None
        or row.state != "running"
        or row.job_id != job.id
        or row.owner_id != job.user_id
        or row.item_id != job.content_plan_item_id
        or row.ownership_epoch != int(job.content_plan_ownership_epoch or 0)
        or row.snapshot_digest
        != _digest({"draft": row.draft_snapshot, "assets": row.asset_snapshot})
    ):
        raise ReceiptConflict("revision_identity_mismatch")
    draft = parse_slide_post(row.draft_snapshot)
    if draft is None or draft.version != row.draft_version:
        raise ReceiptConflict("revision_input_changed")
    return draft, row.asset_snapshot


def stage_result(identifier, result) -> bool:
    with sync_session() as db:
        graph = _locked(db, uuid.UUID(str(identifier)))
        if graph is None or graph[3].state != "running":
            return False
        row = graph[3]
        prefix = artifact_prefix(str(row.job_id), str(row.id)) + "/"
        paths = [
            result.get("video_path"),
            result.get("poster_path"),
            (result.get("slide_post") or {}).get("bundle_gcs_path"),
        ]
        if result.get("render_generation_id") != str(row.id) or any(
            not isinstance(p, str) or not p.startswith(prefix) for p in paths
        ):
            raise ReceiptConflict("revision_result_identity_mismatch")
        if row.staged_result is not None and row.staged_result != result:
            return False
        row.staged_result = copy.deepcopy(result)
        row.next_observe_at = None
        db.commit()
        return True


def publish_result(identifier) -> bool:
    with sync_session() as db:
        graph = _locked(db, uuid.UUID(str(identifier)))
        if graph is None:
            return False
        item, job, parent, revision = graph
        variant = _variant(job)
        if (
            revision.state != "running"
            or revision.staged_result is None
            or parent.execution_state != "completed"
        ):
            return False
        if not variant or variant.get("render_generation_id") != str(revision.id):
            revision.state, revision.error = "superseded", "newer_variant_generation"
            db.commit()
            return False
        if _render_block_reason(job, variant=variant, native_client=revision.native_client):
            _fail(db, graph, "render_policy_blocked")
            return False
        from app.tasks.generative_build import (
            _PENDING_VARIANT_ASSET_FIELDS,
            _reject_unverified_cloud_variant,
        )

        patch = copy.deepcopy(revision.staged_result)
        candidate = {**variant, **patch}
        for evidence_field in ("render_receipt", "cloud_evidence", "duration_s"):
            if evidence_field not in patch:
                candidate.pop(evidence_field, None)
        result = _reject_unverified_cloud_variant(
            job.assembly_plan or {}, candidate, candidates=job.all_candidates or {}
        )
        rejected = result.get("render_status") != "ready" or result.get("ok") is False
        if rejected:
            for field in _PENDING_VARIANT_ASSET_FIELDS:
                if field in variant:
                    result.setdefault(field, variant[field])
            revision.state, revision.error = "failed", "revision_contract_unverified"
        job.assembly_plan = {
            **job.assembly_plan,
            "variants": [
                result if v.get("variant_id") == "slides" else v
                for v in job.assembly_plan["variants"]
            ],
        }
        if not rejected:
            item.slide_post = {**item.slide_post, "rendered_version": revision.draft_version}
            revision.state = "completed"
        db.commit()
        return not rejected


def observe_revision(identifier, *, now=None):
    if not settings.slide_post_generation_recovery_enabled:
        return "disabled"
    now = now or datetime.now(UTC)
    identifier = uuid.UUID(str(identifier))
    finalize = False
    with sync_session() as db:
        graph = _locked(db, identifier)
        if graph is None:
            return "skip"
        _item, job, parent, row = graph
        if parent.execution_state != "completed":
            return "waiting_parent"
        if row.next_observe_at and row.next_observe_at > now:
            return "waiting"
        variant = _variant(job)
        if _render_block_reason(job, variant=variant, native_client=row.native_client):
            row.state, row.error = "blocked", "render_policy_blocked"
            db.commit()
            return "blocked"
        if row.state == "running":
            if row.staged_result is not None:
                if row.finalize_attempts >= 5:
                    _fail(db, graph, "revision_publication_failed")
                    return "failed"
                row.finalize_attempts += 1
                row.next_observe_at = now + timedelta(
                    seconds=min(300, 30 * 2 ** (row.finalize_attempts - 1))
                )
                finalize = True
            elif row.claimed_at + first.RUN_DEADLINE <= now:
                _fail(db, graph, "revision_render_interrupted")
                return "failed"
            else:
                return "waiting"
        else:
            if row.published:
                # The acknowledgement stores the queue deadline in next_observe_at.
                # Save time may precede first-generation completion by many minutes.
                _fail(db, graph, "revision_queue_timeout")
                return "failed"
            if row.publish_attempts >= 5:
                _fail(db, graph, "revision_dispatch_unconfirmed")
                return "failed"
            if variant is None:
                _fail(db, graph, "missing_first_variant")
                return "failed"
            if row.publish_attempts == 0:
                if variant.get("render_status") in {"pending", "rendering"}:
                    try:
                        older_id = uuid.UUID(str(variant.get("render_generation_id")))
                    except ValueError:
                        older_id = None
                    older = db.get(SlidePostRenderRevision, older_id) if older_id else None
                    if (
                        not older
                        or older.parent_request_id != row.parent_request_id
                        or older.draft_version >= row.draft_version
                    ):
                        row.state, row.error = "superseded", "existing_variant_render"
                        db.commit()
                        return "skip"
                    older.state, older.error = "superseded", "newer_saved_draft"
                changed = {
                    **variant,
                    "render_generation_id": str(row.id),
                    "render_status": "rendering",
                }
                job.assembly_plan = {
                    **job.assembly_plan,
                    "variants": [
                        changed if v.get("variant_id") == "slides" else v
                        for v in job.assembly_plan["variants"]
                    ],
                }
            elif variant.get("render_generation_id") != str(row.id):
                row.state, row.error = "superseded", "newer_variant_generation"
                db.commit()
                return "skip"
            row.publish_attempts += 1
            row.next_observe_at = now + timedelta(
                seconds=min(300, 30 * 2 ** (row.publish_attempts - 1))
            )
        job_id = str(job.id)
        db.commit()
    if finalize:
        try:
            return "completed" if publish_result(identifier) else "skip"
        except Exception:
            log.exception("slide_post_revision.finalize_retry", revision=str(identifier))
            return "retry"
    from app.tasks.generative_build import rebuild_slide_post_variant

    try:
        rebuild_slide_post_variant.apply_async(
            args=[job_id],
            kwargs={"render_gen_id": str(identifier), "revision_id": str(identifier)},
            task_id=f"creator-craft-slide-revision-{identifier}",
            queue="plan-jobs",
        )
    except Exception:
        log.exception("slide_post_revision.publish_unknown", revision=str(identifier))
        return "unconfirmed"
    with sync_session() as db:
        row = db.get(SlidePostRenderRevision, identifier, with_for_update=True)
        if row:
            row.published = True
            if row.state == "pending":
                row.next_observe_at = now + first.QUEUE_DEADLINE
            db.commit()
    return "published"


def reconcile_revisions(*, now=None):
    if not settings.slide_post_generation_recovery_enabled:
        return
    now = now or datetime.now(UTC)
    with sync_session() as db:
        # Waiting parents do not consume the eligible dispatch budget. Terminal
        # and missing targets have a separate bounded settlement budget.
        base = (
            select(SlidePostRenderRevision.id)
            .join(
                SlidePostGenerationReceipt,
                SlidePostRenderRevision.parent_request_id
                == SlidePostGenerationReceipt.client_request_id,
            )
            .outerjoin(PlanItem, SlidePostRenderRevision.item_id == PlanItem.id)
            .outerjoin(Job, SlidePostRenderRevision.job_id == Job.id)
            .where(SlidePostRenderRevision.state.in_(["pending", "running"]))
        )
        settlement = list(
            db.scalars(
                base.where(
                    or_(
                        SlidePostGenerationReceipt.execution_state == "failed",
                        Job.status.in_(PLAN_ITEM_JOB_FAILED),
                        Job.id.is_(None),
                        PlanItem.current_job_id.is_distinct_from(SlidePostRenderRevision.job_id),
                        PlanItem.slide_post["version"].as_integer()
                        != SlidePostRenderRevision.draft_version,
                    )
                )
                .order_by(SlidePostRenderRevision.created_at, SlidePostRenderRevision.id)
                .limit(50)
            )
        )
        eligible = list(
            db.scalars(
                base.where(
                    SlidePostGenerationReceipt.execution_state == "completed",
                    or_(
                        SlidePostRenderRevision.next_observe_at.is_(None),
                        SlidePostRenderRevision.next_observe_at <= now,
                    ),
                    or_(
                        SlidePostRenderRevision.state == "pending",
                        SlidePostRenderRevision.staged_result.is_not(None),
                        SlidePostRenderRevision.claimed_at <= now - first.RUN_DEADLINE,
                    ),
                )
                .order_by(
                    SlidePostRenderRevision.next_observe_at.asc().nullsfirst(),
                    SlidePostRenderRevision.id,
                )
                .limit(50)
            )
        )
    for identifier in dict.fromkeys([*settlement, *eligible]):
        try:
            observe_revision(identifier, now=now)
        except Exception:
            log.exception("slide_post_revision.observation_failed", revision=str(identifier))


def with_revision_claim(fn):
    @wraps(fn)
    def wrapped(task, job_id, *args, **kwargs):
        identifier = kwargs.get("revision_id")
        if identifier is None:
            return fn(task, job_id, *args, **kwargs)
        if kwargs.get("render_gen_id") != identifier or not claim_revision(identifier, job_id):
            return None
        from app.pipeline.slide_post.profiles import coerce_platform_profile
        from app.tasks.generative_build import (
            _build_slide_post_result,
            _resolve_slide_post_draft_and_assets,
        )

        try:
            with sync_session() as db:
                _item, _owner, draft, assets = _resolve_slide_post_draft_and_assets(
                    db, job_id, revision_id=identifier
                )
            result = _build_slide_post_result(
                job_id,
                ordered_assets=assets,
                platform_profile=coerce_platform_profile(draft.platform_profile),
                cover_index=min(draft.cover_index, len(assets) - 1),
                caption=draft.caption,
                render_trace_id=uuid.uuid4().hex,
                aspect=draft.aspect,
                artifact_generation=identifier,
                strict_inputs=True,
            )
            if stage_result(identifier, result):
                observe_revision(identifier)
        except Exception:
            with sync_session() as db:
                graph = _locked(db, uuid.UUID(identifier))
                if graph and graph[3].state == "running":
                    _fail(db, graph, "revision_render_failed")
            raise
        return None

    return wrapped
