"""Paid frame review of one finished generation: claim, one provider call, durable outcome.

Every step is a compare-and-swap on the review row. Stages commit before the next one begins,
so a crash at any point is resolved from the row and the cost ledger rather than guessed:

* claim            evidence_ready -> reviewing under the authority locks, lease and paid_call_key
* prepare          no locks: decode the stored JPEGs, build the prompt, estimate, run context
* reserve          cost is reserved before a single frame leaves the process
* recheck          authority locks again: lease, digest, then the reservation is recorded
* send             start fence, one provider call, no locks held
* persist + settle the response is stored before the ledger is settled
* finish           stale check, then the terminal state

No provider call, frame I/O or pipeline event runs while a FOR UPDATE lock is held. An unknown
provider outcome keeps its liability and is never retried.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import func, literal, null, select, update
from sqlalchemy.dialects.postgresql import JSONB

from app.agents._openai_vision import (
    MAX_IMAGES,
    VISION_MODEL,
    OpenAIVisionClient,
    validate_frames,
)
from app.agents._runtime import RunContext, TerminalError
from app.agents._schemas.intent_review import InvalidReviewOutput
from app.config import settings
from app.database import sync_session
from app.kria.intent_review_catalog import ReviewCatalog
from app.kria.intent_review_disposition import evaluate_review
from app.kria.intent_review_tier import intent_review_tier, lower_tier, tier_rank
from app.kria.review_canon import digest as canonical_digest
from app.models import CreatorGenerationReview
from app.services import ai_cost_control as costs
from app.services.bounded_media_review import (
    INTENT_REVIEW_FEATURE,
    MediaReviewRequest,
    MediaReviewResult,
    estimate_media_review_cost,
    frame_set_digest,
    invoke_bounded_media_review,
    reservation_json,
    usage_meter,
)
from app.services.intent_review_prompt import (
    FrameView,
    NothingToReview,
    build_review,
)
from app.services.pipeline_trace import pipeline_trace_for, record_pipeline_event
from app.services.render_review_admission import _inputs, load_review_rows

log = structlog.get_logger()

AGENT_NAME = "nova.kria.intent_review"
CLAIM_DEADLINE_S = 180
LEASE_S = 180
RECOVERY_LEASE_S = 60
REPUBLISH_AFTER_S = 30
MAX_REVIEW_ATTEMPTS = 2
MAX_RAW_OUTPUT_CHARS = 16_384
_URLSAFE = str.maketrans("-_", "+/")
_OWNERSHIP_REASONS = frozenset(
    {"authority_changed", "approved_binding_missing_or_changed", "authority_missing"}
)
_FAILURE_STATUS = {
    "context_too_large": ("unavailable", "context_too_large"),
    "budget_review": ("exhausted", "budget_review"),
    "budget_monthly": ("exhausted", "budget_monthly"),
    "budget_user_day": ("exhausted", "budget_user_day"),
    "paid_gate_closed": ("unavailable", "paid_gate_closed"),
    "ledger_unavailable": ("unavailable", "ledger_unavailable"),
    "provider_unknown_outcome": ("review_unknown", "provider_unknown_outcome"),
    "provider_rejected": ("unavailable", "provider_rejected"),
}


@dataclass(frozen=True)
class ReviewRefs:
    job_id: str
    session_id: str
    turn_id: str
    execution_id: str


@dataclass(frozen=True)
class Claim:
    review_id: uuid.UUID
    execution_id: uuid.UUID
    token: uuid.UUID
    owner_id: uuid.UUID
    paid_call_key: str
    max_usd: float
    refs: ReviewRefs
    packet: dict[str, Any]
    evidence: dict[str, Any]
    restrict_to: tuple[str, ...] | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _lock_row(db, review_id) -> CreatorGenerationReview | None:
    return db.scalar(
        select(CreatorGenerationReview)
        .where(CreatorGenerationReview.id == review_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def check_authority(db, row) -> tuple[Any, str | None]:
    """Lock the authority graph (before the review row) and say why it is no longer current."""
    try:
        rows = load_review_rows(db, row.execution_id, locked=True)
        current = _inputs(rows)
    except ValueError as exc:
        reason = "ownership_changed" if str(exc) in _OWNERSHIP_REASONS else "generation_changed"
        return None, reason
    except (TypeError, KeyError, AttributeError, StopIteration):
        return None, "generation_changed"
    stored = row.inputs
    if (
        canonical_digest(current) == row.input_digest
        and canonical_digest(stored) == row.input_digest
    ):
        return rows, None
    changed_owner = current.get("owner_epoch") != stored.get("owner_epoch") or (
        (current.get("binding") or {}).get("digest") != (stored.get("binding") or {}).get("digest")
    )
    return rows, "ownership_changed" if changed_owner else "generation_changed"


def paid_call_key_for(review_id, refs: ReviewRefs) -> str:
    return costs.logical_call_id(
        feature=INTENT_REVIEW_FEATURE,
        model=VISION_MODEL,
        prompt="",
        ctx=RunContext(
            request_id=f"intent-review:{review_id}",
            request_id_authoritative=True,
            job_id=refs.job_id,
            creator_agent_session_id=refs.session_id,
        ),
        attempt=0,
    )


def intent_review_run_context(row, refs: ReviewRefs) -> RunContext | None:
    """Production attribution for exactly this turn, job and session; None if unattributable."""
    purpose, canary = "optional_background", None
    if costs.is_internal_creator(row.owner_id):
        canary = settings.kria_intent_review_canary_id.strip()
        if not canary:
            return None
        purpose = "internal_canary"
    return RunContext(
        creator_id=str(row.owner_id),
        job_id=refs.job_id,
        creator_agent_session_id=refs.session_id,
        request_id=f"intent-review:{row.id}",
        request_id_authoritative=True,
        usage_purpose=purpose,
        release_canary_id=canary,
        extra={
            "intent_review_id": str(row.id),
            "turn_id": refs.turn_id,
            "execution_id": refs.execution_id,
        },
    )


def _transition(
    db,
    review_id,
    *,
    expect: str,
    status: str,
    reason: str,
    token: uuid.UUID | None = None,
    patch: dict[str, Any] | None = None,
    **values: Any,
) -> bool:
    conditions = [
        CreatorGenerationReview.id == review_id,
        CreatorGenerationReview.status == expect,
    ]
    if token is not None:
        conditions.append(CreatorGenerationReview.lease_token == token)
    fields: dict[str, Any] = dict(
        status=status, reason=reason, lease_token=None, lease_until=None, **values
    )
    if patch:
        fields["review"] = func.coalesce(CreatorGenerationReview.review, literal({}, JSONB)).op(
            "||"
        )(literal(patch, JSONB))
    result = db.execute(
        update(CreatorGenerationReview)
        .where(*conditions)
        .values(**fields)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def finish_row(review_id, *, expect: str, status: str, reason: str, token=None, patch=None, **v):
    with sync_session() as db:
        changed = _transition(
            db,
            review_id,
            expect=expect,
            status=status,
            reason=reason,
            token=token,
            patch=patch,
            **v,
        )
        db.commit()
    return changed


def claim_intent_review(review_id) -> Claim | str:
    with sync_session() as db:
        first = db.get(CreatorGenerationReview, review_id)
        if first is None or first.status != "evidence_ready":
            return "ignored"
        rows, stale = check_authority(db, first)
        row = _lock_row(db, review_id)
        if row.status != "evidence_ready":
            return "ignored"
        live = lower_tier(row.tier, intent_review_tier(row.owner_id))
        age = (_now() - row.evidence_ready_at).total_seconds() if row.evidence_ready_at else None
        verdict: tuple[str, str] | None = None
        if tier_rank(live) < tier_rank("review"):
            verdict = ("unavailable", "paid_disabled")
        elif age is None or age > CLAIM_DEADLINE_S:
            verdict = ("exhausted", "deadline")
        elif stale is not None:
            verdict = ("stale", stale)
        elif row.review_attempts >= MAX_REVIEW_ATTEMPTS:
            verdict = ("exhausted", "attempts")
        elif not row.result or not row.max_usd:
            verdict = ("unavailable", "evidence_missing")
        if verdict is not None:
            row.status, row.reason = verdict
            db.commit()
            return verdict[0]
        from app.services.intent_review_repair_flow import inherited_requirements

        refs = ReviewRefs(
            job_id=str(rows.job.id),
            session_id=str(rows.session.id),
            turn_id=str(rows.turn.id),
            execution_id=str(rows.execution.id),
        )
        token = uuid.uuid4()
        row.status = "reviewing"
        row.review_attempts += 1
        row.lease_token, row.lease_until = token, _now() + timedelta(seconds=LEASE_S)
        row.paid_call_key = paid_call_key_for(row.id, refs)
        claim = Claim(
            review_id=row.id,
            execution_id=row.execution_id,
            token=token,
            owner_id=row.owner_id,
            paid_call_key=row.paid_call_key,
            max_usd=float(row.max_usd),
            refs=refs,
            packet=dict(row.result["packet"]),
            evidence=dict(row.result["evidence"]),
            restrict_to=inherited_requirements(db, row),
        )
        db.commit()
        return claim


class _Unusable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def decode_frames(claim: Claim) -> list[FrameView]:
    evidence = claim.evidence
    frames = evidence.get("frames")
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_IMAGES:
        raise _Unusable("media_corrupt")
    artifact = (evidence.get("verified_artifact") or {}).get("sha256")
    packet_artifact = (claim.packet.get("verified_artifact") or {}).get("sha256")
    decoded: list[tuple[bytes, dict[str, Any]]] = []
    for frame in frames:
        try:
            data = base64.b64decode(frame["image_bytes"].translate(_URLSAFE), validate=True)
            width, height = int(frame["width"]), int(frame["height"])
        except (KeyError, TypeError, ValueError, AttributeError, binascii.Error):
            raise _Unusable("media_corrupt") from None
        if not (1 <= width <= 640 and 1 <= height <= 640):
            raise _Unusable("media_corrupt")
        decoded.append((data, frame))
    try:
        validate_frames([data for data, _ in decoded])
    except TerminalError:
        raise _Unusable("media_corrupt") from None
    views = []
    for index, (data, frame) in enumerate(decoded):
        digest = hashlib.sha256(data).hexdigest()
        if (
            digest != frame.get("image_sha256")
            or not artifact
            or frame.get("artifact_sha256") != artifact
            or packet_artifact != artifact
        ):
            raise _Unusable("frame_hash_mismatch")
        views.append(
            FrameView(
                frame_id=f"f{index}",
                jpeg=data,
                sha256=digest,
                width=int(frame["width"]),
                height=int(frame["height"]),
                timestamp_s=float(frame.get("timestamp_s", 0)),
                anchor_ref=frame.get("anchor_ref"),
            )
        )
    return views


def _own(db, claim: Claim, *, expect: str = "reviewing"):
    row = _lock_row(db, claim.review_id)
    if row is None or row.status != expect or row.lease_token != claim.token:
        return None
    return row


def _terminal_for_claim(claim: Claim, status: str, reason: str, patch=None) -> str:
    changed = finish_row(
        claim.review_id,
        expect="reviewing",
        status=status,
        reason=reason,
        token=claim.token,
        patch=patch,
    )
    return status if changed else "ignored"


def _attribution(claim: Claim) -> dict[str, str]:
    refs = claim.refs
    return {
        "job_id": refs.job_id,
        "session_id": refs.session_id,
        "turn_id": refs.turn_id,
        "execution_id": refs.execution_id,
    }


def _raw_output(raw: str | None) -> tuple[str, bool]:
    text = raw or ""
    return text[:MAX_RAW_OUTPUT_CHARS], len(text) > MAX_RAW_OUTPUT_CHARS


def _call_review(claim: Claim, client: OpenAIVisionClient | None) -> str:
    try:
        frames = decode_frames(claim)
    except _Unusable as exc:
        return _terminal_for_claim(claim, "unavailable", exc.reason)
    try:
        built = build_review(
            claim.packet,
            frames,
            rendered_duration_s=claim.evidence.get("rendered_duration_s"),
            **({"restrict_to": claim.restrict_to} if claim.restrict_to is not None else {}),
        )
    except NothingToReview:
        return _terminal_for_claim(claim, "unavailable", "nothing_verifiable")
    except Exception:
        log.warning("intent_review_prompt_failed", review_id=str(claim.review_id), exc_info=True)
        return _terminal_for_claim(claim, "unavailable", "prompt_builder_unavailable")
    with sync_session() as db:
        row = _own(db, claim)
        if row is None:
            return "ignored"
        context = intent_review_run_context(row, claim.refs)
    if context is None:
        return _terminal_for_claim(claim, "unavailable", "canary_unattributed")
    request = MediaReviewRequest(
        prompt=built.prompt,
        frames=tuple(frame.jpeg for frame in frames),
        frame_dims=tuple((frame.width, frame.height) for frame in frames),
        response_schema=built.response_schema,
        idempotency_key=claim.paid_call_key,
        run_context=context,
        max_usd=claim.max_usd,
        prompt_version=built.prompt_version,
    )
    hashes = tuple(frame.sha256 for frame in frames)
    base_provenance = {
        "frames_sent": 0,
        "frame_hashes": list(hashes),
        "frame_set_digest": frame_set_digest(hashes),
        "visual_input_received": False,
        "provider_outcome": "not_sent",
        "attribution": _attribution(claim),
        "reserved_usd": estimate_media_review_cost(request),
        "price_version": costs.PRICE_VERSION,
    }
    with sync_session() as db:
        row = _own(db, claim)
        if row is None:
            return "ignored"
        row.review = {
            "version": 1,
            "prompt_version": built.prompt_version,
            "model": VISION_MODEL,
            "catalog": built.catalog,
            "provenance": base_provenance,
        }
        db.commit()
    state: dict[str, Any] = {"stale": None, "lost": False}

    def before_transfer(receipt: costs.ReservationReceipt) -> bool:
        with sync_session() as db:
            first = db.get(CreatorGenerationReview, claim.review_id)
            if first is None or first.status != "reviewing" or first.lease_token != claim.token:
                state["lost"] = True
                return False
            _, stale = check_authority(db, first)
            row = _lock_row(db, claim.review_id)
            if row.status != "reviewing" or row.lease_token != claim.token:
                state["lost"] = True
                return False
            if stale is not None:
                state["stale"] = stale
                return False
            row.review = {
                **row.review,
                "provenance": {
                    **row.review["provenance"],
                    "reservation": reservation_json(receipt),
                },
            }
            db.commit()
            return True

    def persist_response(vision, provenance) -> None:
        raw, truncated = _raw_output(vision.raw_text)
        with sync_session() as db:
            row = _own(db, claim)
            if row is None:
                state["lost"] = True
                return
            row.review = {
                **row.review,
                "raw_output": raw,
                "raw_output_truncated": truncated,
                "provenance": {"attribution": _attribution(claim), **provenance.as_json()},
            }
            db.commit()

    result = invoke_bounded_media_review(
        request, client=client, before_transfer=before_transfer, persist_response=persist_response
    )
    return _settle_outcome(claim, result, state)


def _settle_outcome(claim: Claim, result: MediaReviewResult, state: dict[str, Any]) -> str:
    provenance = {"attribution": _attribution(claim), **result.provenance.as_json()}
    if result.status == "ok":
        return _finish(claim.review_id, claim.token, settled=provenance)
    if state["lost"]:
        return "ignored"
    if result.status == "stale":
        return _terminal_for_claim(claim, "stale", state["stale"] or "generation_changed")
    status, reason = _FAILURE_STATUS[result.status]
    patch = {"provenance": provenance}
    if result.detail:
        patch["provenance"] = {**provenance, "detail": result.detail}
    return _terminal_for_claim(claim, status, reason, patch=patch)


def _judge(db, row, review: dict[str, Any], rows) -> tuple[str, str, dict[str, Any]]:
    """Turn a stored, settled response into a terminal state; a void review never lands as clean."""
    try:
        catalog = ReviewCatalog.model_validate(review.get("catalog"))
    except (ValidationError, TypeError):
        return "unavailable", "disposition_unavailable", {}
    if review.get("raw_output_truncated"):
        return "unavailable", "invalid_output", {"invalid_output": "truncated"}
    tier = lower_tier(row.tier, intent_review_tier(row.owner_id))
    if row.parent_review_id is not None:
        tier = lower_tier(tier, "review")
    preconditions = None
    if rows is not None and tier_rank(tier) >= tier_rank("repair"):
        from app.services.intent_review_repair_flow import repair_preconditions

        preconditions = repair_preconditions(db, rows, row)
    try:
        evaluation = evaluate_review(
            review.get("raw_output") or "",
            catalog,
            tier=tier,
            repair_preconditions=preconditions,
        )
    except InvalidReviewOutput as exc:
        return "unavailable", "invalid_output", {"invalid_output": exc.reason}
    except Exception:
        log.warning("intent_review_judgment_failed", review_id=str(row.id), exc_info=True)
        return "unavailable", "disposition_unavailable", {}
    patch: dict[str, Any] = {"findings": [f.model_dump(mode="json") for f in evaluation.findings]}
    if evaluation.disposition is not None:
        patch["disposition"] = evaluation.disposition.model_dump(mode="json")
    return evaluation.status, evaluation.reason, patch


def _finish(review_id, token, *, settled: dict[str, Any] | None = None) -> str:
    """Judge the stored response once the ledger is settled."""
    with sync_session() as db:
        first = db.get(CreatorGenerationReview, review_id)
        if first is None or first.status != "reviewing" or first.lease_token != token:
            return "ignored"
        rows, stale = check_authority(db, first)
        row = _lock_row(db, review_id)
        if row.status != "reviewing" or row.lease_token != token:
            return "ignored"
        review = dict(row.review or {})
        if settled is not None:
            review["provenance"] = settled
        provenance = review.get("provenance") or {}
        if stale is not None:
            verdict = ("stale", stale)
        elif provenance.get("response_status") != "completed":
            verdict = ("unavailable", "invalid_output")
        else:
            status, reason, patch = _judge(db, row, review, rows)
            review.update(patch)
            verdict = (status, reason)
            if status == "repair_staged":
                row.review = review
                row.lease_token = uuid.uuid4()
                row.lease_until = _now() + timedelta(seconds=LEASE_S)
                db.commit()
                return "repair_pending"
        row.review = review
        row.status, row.reason = verdict
        row.lease_token = row.lease_until = None
        db.commit()
        return verdict[0]


def run_intent_review(review_id, *, client: OpenAIVisionClient | None = None) -> str:
    with sync_session() as db:
        peek = db.get(CreatorGenerationReview, review_id)
        status = peek.status if peek is not None else None
        expired = bool(peek and peek.lease_until and peek.lease_until <= _now())
    if status == "reviewing":
        return _recover_expired(review_id) if expired else "ignored"
    if status != "evidence_ready":
        return "ignored"
    claimed = claim_intent_review(review_id)
    if not isinstance(claimed, Claim):
        outcome = claimed
    else:
        outcome = _stage_repair_if_pending(review_id, _call_review(claimed, client))
    if outcome != "ignored":
        from app.services.intent_review_repair_flow import settle_parent_from_child

        _emit_trace(review_id, outcome)
        post_intent_review_outcome(review_id)
        settle_parent_from_child(review_id)
    return outcome


def _stage_repair_if_pending(review_id, outcome: str) -> str:
    if outcome != "repair_pending":
        return outcome
    from app.services.intent_review_repair_flow import stage_intent_repair

    with sync_session() as db:
        token = db.scalar(
            select(CreatorGenerationReview.lease_token).where(
                CreatorGenerationReview.id == review_id
            )
        )
    return stage_intent_repair(review_id, token=token)


def post_intent_review_outcome(review_id) -> str:
    """Tell the creator what the review found, once and only where it is still true.

    Silent outcomes post nothing; the deterministic post-render receipt stands for them.
    """
    from app.services.intent_review_post import post_intent_review_outcome as post

    return post(review_id)


def _emit_trace(review_id, outcome: str) -> None:
    with sync_session() as db:
        row = db.get(CreatorGenerationReview, review_id)
        if row is None:
            return
        review = row.review or {}
        provenance = review.get("provenance") or {}
        basis = row.inputs.get("basis") or {}
        data = {
            "review_id": str(row.id),
            "status": row.status,
            "reason": row.reason,
            "tier": row.tier,
            "review_attempts": row.review_attempts,
            "outcome": outcome,
        }
        prompt_version = review.get("prompt_version") or "unversioned"
        raw, error = review.get("raw_output"), row.reason
        voided = row.reason == "invalid_output"
        verdicts = {
            "findings": review.get("findings"),
            "disposition": review.get("disposition"),
        }
        catalog_digest = (review.get("catalog") or {}).get("digest")
        fidelity = [
            item["requirement_id"]
            for item in (review.get("disposition") or {}).get("suggestions") or []
            if item.get("why") == "render_fidelity"
        ]
        if review.get("invalid_output"):
            data["invalid_output"] = review["invalid_output"]
    with pipeline_trace_for(basis.get("job_id")):
        record_pipeline_event("intent_review", "outcome", data)
        for requirement_id in fidelity:
            record_pipeline_event(
                "intent_review",
                "render_fidelity",
                {"review_id": str(review_id), "requirement_id": requirement_id},
            )
    if provenance.get("provider_outcome") not in {"ok", "rejected", "unknown"}:
        return
    from app.agents._persistence import persist_agent_run

    usage = provenance.get("usage") or {}
    reservation = provenance.get("reservation") or {}
    settled = float(provenance.get("settled_usd") or 0)
    persist_agent_run(
        job_id=basis.get("job_id"),
        segment_idx=None,
        agent_name=AGENT_NAME,
        prompt_version=prompt_version,
        model=VISION_MODEL,
        outcome=_agent_outcome(provenance, voided=voided),
        attempts=1,
        tokens_in=int(usage.get("input_tokens", 0)),
        tokens_out=int(usage.get("output_tokens", 0)),
        token_details={"reasoning_tokens": int(usage.get("reasoning_tokens", 0))},
        usage_purpose=reservation.get("usage_purpose"),
        provider_request_id=provenance.get("response_id"),
        price_version=provenance.get("price_version"),
        reserved_cost_usd=float(provenance.get("reserved_usd") or 0),
        settled_cost_usd=settled,
        cost_reservation_id=reservation.get("id"),
        cost_usd=settled,
        latency_ms=int(provenance.get("latency_ms") or 0),
        input_dict={
            "frame_hashes": provenance.get("frame_hashes"),
            "frame_set_digest": provenance.get("frame_set_digest"),
            "catalog_digest": catalog_digest,
        },
        output_dict=None if voided or verdicts["findings"] is None else verdicts,
        raw_text=raw,
        error=None if provenance.get("provider_outcome") == "ok" else error,
    )


def _agent_outcome(provenance: dict[str, Any], *, voided: bool = False) -> str:
    if provenance.get("provider_outcome") != "ok":
        return "terminal_transient"
    status = provenance.get("response_status")
    if status == "completed":
        return "terminal_schema" if voided else "ok"
    return "terminal_refusal" if status == "refused" else "terminal_transient"


def review_job_id(review_id) -> str | None:
    with sync_session() as db:
        row = db.get(CreatorGenerationReview, review_id)
        return (row.inputs.get("basis") or {}).get("job_id") if row else None


def publish_intent_review(review_id) -> bool:
    if not settings.kria_intent_review_enabled:
        return False
    from app.tasks.render_review import review_generation_intent

    try:
        with sync_session() as db:
            row = _lock_row(db, review_id)
            if row is None or row.status != "evidence_ready":
                return False
            row.last_publish_attempt_at = db.scalar(select(func.clock_timestamp()))
            db.commit()
        review_generation_intent.apply_async(
            args=[str(review_id)],
            task_id=f"generation-review:{review_id}:intent",
            queue="agent-control",
        )
        return True
    except Exception:
        log.warning("intent_review_publish_failed", review_id=str(review_id), exc_info=True)
        return False


def recover_intent_reviews(deadline: float | None = None) -> int:
    """Bounded by ``deadline`` (monotonic): never starts a row once the beat budget is spent."""
    if not settings.kria_intent_review_enabled:
        return 0
    now = _now()
    ready_before = now - timedelta(seconds=REPUBLISH_AFTER_S)
    with sync_session() as db:
        waiting = list(
            db.execute(
                select(CreatorGenerationReview.id, CreatorGenerationReview.evidence_ready_at)
                .where(
                    CreatorGenerationReview.status == "evidence_ready",
                    CreatorGenerationReview.tier.in_(("review", "repair")),
                    CreatorGenerationReview.evidence_ready_at < ready_before,
                    (CreatorGenerationReview.last_publish_attempt_at.is_(None))
                    | (CreatorGenerationReview.last_publish_attempt_at < ready_before),
                )
                .order_by(CreatorGenerationReview.evidence_ready_at)
                .limit(25)
            )
        )
        expired = list(
            db.scalars(
                select(CreatorGenerationReview.id)
                .where(
                    CreatorGenerationReview.status == "reviewing",
                    CreatorGenerationReview.lease_until < now,
                )
                .limit(25)
            )
        )
    handled = 0

    def time_left() -> bool:
        return deadline is None or time.monotonic() < deadline

    for identifier, ready_at in waiting:
        if not time_left():
            return handled
        if (now - ready_at).total_seconds() > CLAIM_DEADLINE_S:
            handled += finish_row(
                identifier, expect="evidence_ready", status="exhausted", reason="deadline"
            )
        else:
            handled += publish_intent_review(identifier)
    for identifier in expired:
        if not time_left():
            return handled
        _recover_expired(identifier)
        handled += 1
    from app.services.intent_review_post import post_unposted_outcomes
    from app.services.intent_review_repair_flow import recover_repair_staged

    handled += recover_repair_staged()
    return handled + post_unposted_outcomes()


def _recover_expired(review_id) -> str:
    outcome = _stage_repair_if_pending(review_id, _resolve_expired(review_id))
    if outcome not in {"ignored", "evidence_ready"}:
        _emit_trace(review_id, outcome)
        post_intent_review_outcome(review_id)
    return outcome


def _resolve_expired(review_id) -> str:
    """Resolve an abandoned lease from the stored response or the ledger, never by re-calling."""
    from app.services.intent_review_repair_flow import stage_intent_repair, wants_staging

    new_token = uuid.uuid4()
    with sync_session() as db:
        row = _lock_row(db, review_id)
        if row is None or row.status != "reviewing" or row.lease_until >= _now():
            return "ignored"
        key = row.paid_call_key
        stored = row.review or {}
        attempts = row.review_attempts
        staging = wants_staging(row)
        row.lease_token = new_token
        row.lease_until = _now() + timedelta(seconds=RECOVERY_LEASE_S)
        db.commit()
    if staging:
        return stage_intent_repair(review_id, token=new_token)
    ledger = costs.reservation_for_key(key)
    if stored.get("raw_output") is not None:
        return _resume_after_response(review_id, new_token, stored, ledger, key)
    status = ledger[1] if ledger else None
    receipt = ledger[0] if ledger else None
    if status == "provider_started":
        costs.mark_paid_call_unknown(receipt)
        return _recovered(review_id, new_token, "review_unknown", "provider_unknown_outcome")
    if status == "unknown":
        return _recovered(review_id, new_token, "review_unknown", "provider_unknown_outcome")
    if status == "settled":
        return _recovered(review_id, new_token, "review_unknown", "response_lost")
    if status == "reserved":
        costs.release_paid_call(receipt)
    if attempts >= MAX_REVIEW_ATTEMPTS:
        return _recovered(review_id, new_token, "exhausted", "attempts")
    with sync_session() as db:
        reset = db.execute(
            update(CreatorGenerationReview)
            .where(
                CreatorGenerationReview.id == review_id,
                CreatorGenerationReview.status == "reviewing",
                CreatorGenerationReview.lease_token == new_token,
            )
            .values(
                status="evidence_ready",
                evidence_ready_at=_now(),
                lease_token=None,
                lease_until=None,
                review=null(),
            )
        ).rowcount
        db.commit()
    if reset == 1:
        publish_intent_review(review_id)
        return "evidence_ready"
    return "ignored"


def _recovered(review_id, token, status: str, reason: str) -> str:
    finish_row(review_id, expect="reviewing", status=status, reason=reason, token=token)
    return status


def _resume_after_response(review_id, token, stored, ledger, key) -> str:
    provenance = stored.get("provenance") or {}
    if ledger is None or ledger[1] in {"released", "reserved"}:
        return _recovered(review_id, token, "review_unknown", "response_lost")
    receipt, status = ledger
    settled = provenance
    if status in {"provider_started", "unknown"}:
        cost = costs.settle_paid_call(
            receipt, usage_meter(provenance.get("usage") or {}, provenance.get("response_id"))
        )
        settled = {**provenance, "settled_usd": cost}
    elif status == "settled" and provenance.get("settled_usd") is None:
        booked = costs.settled_cost_for_key(key)
        if booked is not None:
            settled = {**provenance, "settled_usd": booked}
    return _finish(review_id, token, settled=settled)
