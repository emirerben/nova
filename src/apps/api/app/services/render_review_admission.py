"""Canonical advisory evidence outbox. No model calls, render grants or editor writes."""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.database import sync_session
from app.kria.brief_binding import BriefBinding
from app.kria.drafts import KriaDraftDocument, canonical_snapshot
from app.kria.render_review_context import ReviewRows, build_render_review_context
from app.models import (
    ContentPlan,
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    CreatorGenerationReview,
    CreatorPreparedExecution,
    Job,
    PlanItem,
)
from app.services.device_render import device_record, device_status

log = structlog.get_logger()
MAX_ATTEMPTS = 3
LEASE_SECONDS = 120


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def load_review_rows(db, execution_id, *, locked=False):
    e = db.get(CreatorAgentExecution, execution_id)
    if e is None:
        raise ValueError("execution_missing")
    s = db.get(CreatorAgentSession, e.session_id)
    i = db.get(PlanItem, s.plan_item_id)
    approval_id = uuid.UUID(e.result["approval_id"])

    def get(model, key):
        if key is None:
            raise ValueError("authority_missing")
        query = select(model).where(model.id == key).execution_options(populate_existing=True)
        if locked:
            query = query.with_for_update()
        row = db.scalar(query)
        if row is None:
            raise ValueError("authority_missing")
        return row

    p = get(ContentPlan, i.content_plan_id)
    i = get(PlanItem, i.id)
    j = get(Job, e.target_job_id)
    s = get(CreatorAgentSession, e.session_id)
    turn = get(CreatorAgentTurn, e.turn_id)
    d = get(CreatorEditDraft, e.target_draft_id)
    a = get(CreatorAgentApproval, approval_id)
    e = get(CreatorAgentExecution, e.id)
    t = get(CreationThread, e.target_thread_id)
    prepared = (
        db.get(CreatorPreparedExecution, a.prepared_execution_id)
        if a.prepared_execution_id
        else None
    )
    return ReviewRows(p, i, j, t, s, turn, d, a, e, prepared)


def _inputs(rows):
    p, i, j, t, s, e, d, a = (
        rows.plan,
        rows.item,
        rows.job,
        rows.thread,
        rows.session,
        rows.execution,
        rows.draft,
        rows.approval,
    )
    if not (
        t.runtime_version == 2
        and t.active_plan_item_id == i.id
        and t.active_creator_agent_session_id == s.id
        and p.user_id == t.creator_id == j.user_id == s.creator_id
        and i.content_plan_id == p.id
        and i.current_job_id == j.id == s.target_job_id == e.target_job_id
        and j.content_plan_item_id == i.id == s.plan_item_id == d.item_id
        and p.ownership_epoch
        == j.content_plan_ownership_epoch
        == s.ownership_epoch
        == e.target_ownership_epoch
        == a.target_ownership_epoch
        and e.target_thread_id == t.id == d.thread_id == a.thread_id
        and rows.turn.id == e.turn_id == a.turn_id
        and rows.turn.thread_id == t.id
        and rows.turn.session_id == s.id == e.session_id == a.session_id
        and e.status == "completed"
        and e.tool_name == "render.request"
        and a.status in {"approved", "consumed"}
        and str(e.id) in a.execution_ids
        and d.id == e.target_draft_id == a.draft_id
        and d.draft_revision == e.target_draft_revision == a.draft_revision
    ):
        raise ValueError("authority_changed")
    binding = BriefBinding.model_validate(e.result.get("brief_binding"))
    document = KriaDraftDocument.model_validate(d.snapshot_json)
    if (
        canonical_snapshot(document)[1] != d.snapshot_hash
        or document.brief_binding is None
        or document.brief_binding.digest != binding.digest
        or binding.thread_id != str(t.id)
        or binding.state != "pinned"
        or not binding.creator_request
    ):
        raise ValueError("approved_binding_missing_or_changed")
    initial_authority = None
    if document.kind == "strategy":
        from app.kria.initial_render_review_authority import resolve_initial_render_authority

        initial_authority = resolve_initial_render_authority(rows)
    variant_id = e.target_variant_id
    record = device_record(j, variant_id)
    status = device_status(j, variant_id)
    variant = next(v for v in j.assembly_plan["variants"] if v.get("variant_id") == variant_id)
    if (
        status.phase != "published"
        or variant.get("render_status") != "ready"
        or status.published_generation != variant.get("render_generation_id")
        or record.get("requirement_generation")
        != (
            initial_authority.requirement_generation
            if initial_authority
            else e.target_generation_id
        )
    ):
        raise ValueError("publication_changed")
    attempt = record.get("attempts", {}).get(status.published_generation, {})
    basis = dict(
        execution_id=str(e.id),
        owner_id=str(t.creator_id),
        job_id=str(j.id),
        variant_id=variant_id,
        published_attempt=status.published_generation,
        requirement_generation=record.get("requirement_generation"),
        recipe_digest=status.request.identity.recipe_digest,
        recipe_identity_json=json.dumps(
            status.request.identity.model_dump(mode="json"), sort_keys=True
        ),
        object_path=variant.get("video_path"),
        publication_proof_json=json.dumps(attempt.get("publication_proof"), sort_keys=True),
    )
    return dict(
        basis=basis,
        owner_epoch=s.ownership_epoch,
        draft_id=str(d.id),
        draft_revision=d.draft_revision,
        draft_hash=d.snapshot_hash,
        draft=copy.deepcopy(d.snapshot_json),
        binding=binding.model_dump(mode="json"),
        approval_id=str(a.id),
        recipe=status.request.recipe.model_dump(mode="json"),
        dispatch_settings=copy.deepcopy((j.all_candidates or {}).get("creator_strategy")),
        record=record,
        prepared=copy.deepcopy(rows.prepared.snapshot) if rows.prepared else None,
        prepared_digest=rows.prepared.snapshot_digest if rows.prepared else None,
        prepared_invalidated=str(rows.prepared.invalidated_at)
        if rows.prepared and rows.prepared.invalidated_at
        else None,
    )


def _demand_identity(execution, job, session):
    return _digest(
        {
            "execution": str(execution.id),
            "job": str(job.id),
            "owner": str(session.creator_id),
            "epoch": session.ownership_epoch,
            "variant": execution.target_variant_id,
            "published": execution.result.get("render_generation_id"),
            "draft": str(execution.target_draft_id),
            "revision": execution.target_draft_revision,
            "binding": execution.result.get("brief_binding"),
            "record": device_record(job, execution.target_variant_id),
            "variants": (job.assembly_plan or {}).get("variants"),
            "settings": (job.all_candidates or {}).get("creator_strategy"),
        }
    )


def mark_admission_demand(execution, job, session):
    """Persist alongside readiness, before the fallible admission savepoint."""
    try:
        identity = _demand_identity(execution, job, session)
    except (KeyError, ValueError, TypeError, AttributeError):
        # Missing native metadata is an advisory capability gap, not a render failure.
        # Do not catch SQLAlchemy/database errors: readiness still needs a real commit.
        marker = {
            "status": "unavailable",
            "reason": "native_authority_unavailable",
            "attempts": 0,
            "job_id": str(job.id),
            "variant_id": execution.target_variant_id,
            "published_attempt": execution.result.get("render_generation_id"),
        }
    else:
        marker = {"status": "pending", "attempts": 0, "identity": identity}
    execution.result = {**execution.result, "intent_review_admission": marker}


def _set_admission_marker(execution, **changes):
    marker = execution.result.get("intent_review_admission")
    if marker is not None:
        execution.result = {**execution.result, "intent_review_admission": {**marker, **changes}}


def _recover_admission_demands():
    with sync_session() as db:
        identifiers = list(
            db.scalars(
                select(CreatorAgentExecution.id)
                .where(
                    text(
                        "status = 'completed' AND "
                        "result->'intent_review_admission'->>'status' = 'pending'"
                    ),
                )
                .order_by(CreatorAgentExecution.completed_at)
                .limit(25)
            )
        )
    for identifier in identifiers:
        with sync_session() as db:
            try:
                rows = load_review_rows(db, identifier, locked=True)
                execution = rows.execution
                marker = execution.result.get("intent_review_admission", {})
                if marker.get("status") != "pending":
                    continue
                recovered_id = None
                if marker.get("identity") != _demand_identity(execution, rows.job, rows.session):
                    _set_admission_marker(execution, status="stale")
                else:
                    _inputs(rows)  # Recheck the entire current ownership/approved authority graph.
                    recovered_id = admit_generation_review(db, execution)
                db.commit()
                if recovered_id is not None:
                    publish_generation_review(recovered_id)
            except (ValueError, TypeError, KeyError, AttributeError, StopIteration):
                db.rollback()
                execution = db.get(CreatorAgentExecution, identifier, with_for_update=True)
                if (
                    execution
                    and execution.result.get("intent_review_admission", {}).get("status")
                    == "pending"
                ):
                    _set_admission_marker(execution, status="stale")
                    db.commit()
            except Exception:
                # Database outages retain the demand; the next bounded scan can retry.
                db.rollback()
                log.warning(
                    "intent_review_admission_recovery_failed",
                    execution_id=str(identifier),
                    exc_info=True,
                )


def admit_generation_review(db, execution):
    """Caller owns the readiness transaction and graph locks. Never publish here."""
    if not settings.kria_intent_review_enabled:
        return None
    marker = execution.result.get("intent_review_admission")
    if marker is not None:
        if marker.get("status") != "pending":
            return None
        if marker.get("attempts", 0) >= MAX_ATTEMPTS:
            _set_admission_marker(execution, status="exhausted")
            return None
        _set_admission_marker(execution, attempts=marker.get("attempts", 0) + 1)
    try:
        with db.begin_nested():
            owner_session = db.get(CreatorAgentSession, execution.session_id)
            try:
                rows = load_review_rows(db, execution.id)
                inputs = _inputs(rows)
                if len(json.dumps(inputs).encode()) > 2 * 1024 * 1024:
                    raise ValueError("admission_context_too_large")
                state, reason = "pending", None
            except (ValueError, TypeError, KeyError, AttributeError, StopIteration):
                inputs = {"execution_id": str(execution.id), "status": "missing_authority"}
                state, reason = "unavailable", "missing_authority"
            identity = inputs.get("basis", {})
            key = _digest(
                [
                    str(execution.id),
                    identity.get("published_attempt"),
                    identity.get("recipe_digest"),
                    str(owner_session.creator_id),
                    owner_session.ownership_epoch,
                    inputs.get("binding", {}).get("digest"),
                ]
            )
            identifier = uuid.uuid4()
            db.execute(
                insert(CreatorGenerationReview)
                .values(
                    id=identifier,
                    execution_id=execution.id,
                    owner_id=owner_session.creator_id,
                    review_key=key,
                    input_digest=_digest(inputs),
                    inputs=inputs,
                    status=state,
                    attempts=0,
                    reason=reason,
                )
                .on_conflict_do_nothing(index_elements=["review_key"])
            )
            row = db.scalar(
                select(CreatorGenerationReview).where(CreatorGenerationReview.review_key == key)
            )
            _set_admission_marker(execution, status="admitted", review_id=str(row.id))
            return row.id if row.status == "pending" else None
    except Exception:
        # Advisory admission must never roll back a successfully published video.
        log.warning(
            "intent_review_admission_unavailable", execution_id=str(execution.id), exc_info=True
        )
        return None


def publish_generation_review(review_id):
    if not settings.kria_intent_review_enabled:
        return False
    from app.tasks.render_review import prepare_generation_review

    try:
        # Rotate even failed broker attempts; older undelivered rows cannot monopolize a batch.
        # Commit the scheduling cursor before network I/O, independently of worker claim leases.
        with sync_session() as db:
            row = db.get(CreatorGenerationReview, review_id, with_for_update=True)
            if row is None or row.status not in {"pending", "running"}:
                return False
            row.last_publish_attempt_at = db.scalar(select(func.clock_timestamp()))
            db.commit()
        prepare_generation_review.apply_async(
            args=[str(review_id)], task_id=f"generation-review:{review_id}", queue="agent-control"
        )
        return True
    except Exception:
        log.warning("intent_review_publish_failed", review_id=str(review_id), exc_info=True)
        return False


def reconcile_generation_reviews():
    if not settings.kria_intent_review_enabled:
        return 0
    _recover_admission_demands()
    with sync_session() as db:
        ids = list(
            db.scalars(
                select(CreatorGenerationReview.id)
                .where(
                    or_(
                        CreatorGenerationReview.status == "pending",
                        (CreatorGenerationReview.status == "running")
                        & (CreatorGenerationReview.lease_until < datetime.now(UTC)),
                    )
                )
                .order_by(
                    CreatorGenerationReview.last_publish_attempt_at.asc().nulls_first(),
                    CreatorGenerationReview.created_at,
                    CreatorGenerationReview.id,
                )
                .limit(25)
            )
        )
    return sum(publish_generation_review(identifier) for identifier in ids)


def claim_generation_review(review_id):
    if not settings.kria_intent_review_enabled:
        return None
    with sync_session() as db:
        row = db.get(CreatorGenerationReview, review_id, with_for_update=True)
        now = datetime.now(UTC)
        if (
            row is None
            or row.status not in {"pending", "running"}
            or (row.status == "running" and row.lease_until and row.lease_until > now)
        ):
            return None
        if _digest(row.inputs) != row.input_digest:
            row.status, row.reason = "stale", "admission_inputs_changed"
            row.lease_token, row.lease_until = None, None
            db.commit()
            return None
        if row.attempts >= MAX_ATTEMPTS:
            row.status, row.reason = "exhausted", "attempt_limit"
            row.lease_token, row.lease_until = None, None
            db.commit()
            return None
        token = uuid.uuid4()
        row.status, row.attempts = "running", row.attempts + 1
        row.lease_token, row.lease_until = token, now + timedelta(seconds=LEASE_SECONDS)
        claim = (row.execution_id, token, row.input_digest, copy.deepcopy(row.inputs))
        db.commit()
        return claim


def run_generation_review(review_id):
    claim = claim_generation_review(review_id)
    if claim is None:
        return "ignored"
    execution_id, token, input_digest, inputs = claim
    from app.services.render_review_evidence import ReviewBasis, obtain_render_review_evidence

    # Recheck before storage I/O so a known stale generation consumes no evidence work.
    try:
        with sync_session() as db:
            if _digest(_inputs(load_review_rows(db, execution_id))) != input_digest:
                raise ValueError("authority_changed")
        evidence = obtain_render_review_evidence(
            ReviewBasis.model_validate(inputs["basis"]), deadline_monotonic=time.monotonic() + 90
        )
        bundle = evidence.bundle if evidence.status == "ready" else None
        evidence_json = bundle.model_dump(mode="json") if bundle else None
        if evidence_json and len(json.dumps(evidence_json).encode()) > 2 * 1024 * 1024:
            raise ValueError("evidence_bundle_too_large")
        reason = evidence.reason
    except (ValueError, TypeError, KeyError, AttributeError, StopIteration) as exc:
        bundle, evidence_json, reason = None, None, "authority_or_evidence_unavailable"
        log.info("intent_review_input_unavailable", error_type=type(exc).__name__)
    except Exception:
        # Leave a pending row for bounded retry; never infer unknown provider success.
        with sync_session() as db:
            row = db.get(CreatorGenerationReview, review_id, with_for_update=True)
            if row and row.lease_token == token:
                row.status, row.reason = "pending", "evidence_retryable"
                row.lease_token, row.lease_until = None, None
                db.commit()
        return "pending"
    with sync_session() as db:
        try:
            rows = load_review_rows(db, execution_id, locked=True)
            current = _inputs(rows)
            current_digest = _digest(current)
        except (ValueError, TypeError, KeyError, AttributeError, StopIteration):
            rows, current_digest = None, None
        row = db.get(CreatorGenerationReview, review_id, with_for_update=True)
        if (
            row is None
            or row.status != "running"
            or row.lease_token != token
            or row.lease_until <= datetime.now(UTC)
        ):
            return "ignored"
        if (
            current_digest != input_digest
            or row.input_digest != input_digest
            or _digest(row.inputs) != input_digest
        ):
            row.status, row.reason = "stale", "authority_changed"
        elif bundle is None:
            row.status, row.reason = "unavailable", reason or "evidence_unavailable"
        else:
            capture = build_render_review_context(
                rows, verified_artifact=bundle.verified_artifact, observations=bundle.observations
            )
            if capture.status == "ready":
                row.status, row.reason = "evidence_ready", None
                row.result = {
                    "packet": capture.packet.model_dump(mode="json"),
                    "evidence": evidence_json,
                    "review_model_received_visual_input": False,
                }
                if len(json.dumps(row.result).encode()) > 4 * 1024 * 1024:
                    row.status, row.reason, row.result = (
                        "unavailable",
                        "review_result_too_large",
                        None,
                    )
            else:
                row.status, row.reason = "unavailable", capture.reason
        row.lease_token, row.lease_until = None, None
        db.commit()
        return row.status
