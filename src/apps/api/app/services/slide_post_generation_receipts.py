"""Durable request identity; retries observe and never republish a render.

The receipt and Job commit together. Broker publication cannot share that
transaction: a process lost in between remains explicitly unconfirmed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import sync_session
from app.models import Job, PlanItem, PlanItemAsset, SlidePostGenerationReceipt
from app.schemas.slide_post import SlidePostDraft, parse_slide_post, sticker_asset_ids
from app.schemas.slide_post_generation import SlidePostGenerationReceiptOut

log = structlog.get_logger()


class ReceiptConflict(ValueError):
    """The key or immutable render input does not match its authority."""


def _digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class SlidePostGenerationRequest:
    client_request_id: uuid.UUID
    owner_id: uuid.UUID
    item_id: uuid.UUID
    expected_version: int
    expected_current_job_id: uuid.UUID | None = None

    @property
    def digest(self) -> str:
        return _digest(
            {
                "operation": "slide_post_generation_v1",
                "owner_id": str(self.owner_id),
                "item_id": str(self.item_id),
                "expected_version": self.expected_version,
                "expected_current_job_id": str(self.expected_current_job_id)
                if self.expected_current_job_id
                else None,
            }
        )


def validate_receipt(row: SlidePostGenerationReceipt, request: SlidePostGenerationRequest) -> None:
    if (
        row.client_request_id != request.client_request_id
        or row.owner_id != request.owner_id
        or row.item_id != request.item_id
        or row.draft_version != request.expected_version
        or row.request_digest != request.digest
    ):
        raise ReceiptConflict("slide_post_request_conflict")


def receipt_output(
    row: SlidePostGenerationReceipt, *, item: PlanItem, job: Job | None
) -> SlidePostGenerationReceiptOut:
    if job is not None and (
        job.id != row.job_id
        or job.user_id != row.owner_id
        or job.content_plan_item_id != row.item_id
    ):
        raise ReceiptConflict("slide_post_request_conflict")
    return SlidePostGenerationReceiptOut(
        client_request_id=row.client_request_id,
        item_id=row.item_id,
        draft_version=row.draft_version,
        job_id=row.job_id,
        dispatch_state=row.dispatch_state,
        job_status=job.status if job else None,
        is_current_target=item.current_job_id == row.job_id and job is not None,
    )


def lookup_receipt(
    db: Session, request: SlidePostGenerationRequest, *, item: PlanItem
) -> SlidePostGenerationReceiptOut | None:
    row = db.get(SlidePostGenerationReceipt, request.client_request_id)
    if row is None:
        return None
    validate_receipt(row, request)
    return receipt_output(row, item=item, job=db.get(Job, row.job_id))


def capture_inputs(db: Session, item: PlanItem, draft: SlidePostDraft, owner_id: uuid.UUID):
    asset_ids = {ref.asset_id for ref in draft.slides} | set(sticker_asset_ids(draft))
    rows = db.execute(select(PlanItemAsset).where(PlanItemAsset.id.in_(asset_ids))).scalars().all()
    assets = {str(row.id): row for row in rows}
    prefix = f"users/{owner_id}/plan/{item.id}/"
    if any(
        str(asset_id) not in assets
        or assets[str(asset_id)].user_id != owner_id
        or assets[str(asset_id)].plan_item_id != item.id
        or assets[str(asset_id)].media_status != "ready"
        or not assets[str(asset_id)].gcs_generation
        or not assets[str(asset_id)].gcs_path.startswith(prefix)
        for asset_id in asset_ids
    ):
        raise ReceiptConflict("slide_post_input_changed")
    if any(assets[str(ref.asset_id)].kind != ref.kind for ref in draft.slides):
        raise ReceiptConflict("slide_post_input_changed")
    snapshot = draft.model_dump(mode="json")
    asset_snapshot = {
        key: {"path": row.gcs_path, "generation": row.gcs_generation, "kind": row.kind}
        for key, row in assets.items()
    }
    return snapshot, asset_snapshot


def capture_receipt(
    db: Session,
    request: SlidePostGenerationRequest,
    *,
    item: PlanItem,
    job: Job,
    draft: SlidePostDraft,
    ownership_epoch: int,
) -> SlidePostGenerationReceipt:
    if (
        item.id != request.item_id
        or job.user_id != request.owner_id
        or job.content_plan_item_id != item.id
        or draft.version != request.expected_version
    ):
        raise ReceiptConflict("slide_post_request_conflict")
    snapshot, asset_snapshot = capture_inputs(db, item, draft, request.owner_id)
    row = SlidePostGenerationReceipt(
        client_request_id=request.client_request_id,
        owner_id=request.owner_id,
        item_id=item.id,
        ownership_epoch=ownership_epoch,
        draft_version=request.expected_version,
        request_digest=request.digest,
        job_id=job.id,
        draft_snapshot=snapshot,
        asset_snapshot=asset_snapshot,
        snapshot_digest=_digest({"draft": snapshot, "assets": asset_snapshot}),
        dispatch_state="unconfirmed",
    )
    db.add(row)
    return row


def first_render_snapshot(db: Session, job: Job) -> tuple[SlidePostDraft, dict] | None:
    row = db.execute(
        select(SlidePostGenerationReceipt).where(SlidePostGenerationReceipt.job_id == job.id)
    ).scalar_one_or_none()
    if row is None:
        return None
    if (
        row.owner_id != job.user_id
        or row.item_id != job.content_plan_item_id
        or row.ownership_epoch != int(job.content_plan_ownership_epoch or 0)
        or row.snapshot_digest
        != _digest({"draft": row.draft_snapshot, "assets": row.asset_snapshot})
    ):
        raise ReceiptConflict("slide_post_input_changed")
    draft = parse_slide_post(row.draft_snapshot)
    if draft is None or draft.version != row.draft_version:
        raise ReceiptConflict("slide_post_input_changed")
    return draft, row.asset_snapshot


def record_publication(request_id: uuid.UUID, *, published: bool) -> None:
    """Metadata only; failure to record an acknowledgement never retries publication."""
    try:
        with sync_session() as db:
            db.execute(
                update(SlidePostGenerationReceipt)
                .where(
                    SlidePostGenerationReceipt.client_request_id == request_id,
                    SlidePostGenerationReceipt.dispatch_state == "unconfirmed",
                )
                .values(dispatch_state="published" if published else "publish_failed")
            )
            db.commit()
    except Exception:
        log.exception(
            "slide_post_receipt.publication_receipt_unconfirmed", request_id=str(request_id)
        )


def dispatch_generation(request: SlidePostGenerationRequest, ownership_epoch: int):
    """A global UUID race loses the entire mint transaction, then observes the winner."""
    from app.tasks.content_plan_build import dispatch_item_render_for

    try:
        result = dispatch_item_render_for(
            str(request.item_id),
            ownership_epoch,
            expected_slide_post_version=request.expected_version,
            slide_post_request=request,
        )
        if result.job_id is None or result.generation_receipt is not None:
            return result
        return dispatch_item_render_for(
            str(request.item_id),
            ownership_epoch,
            expected_slide_post_version=request.expected_version,
            slide_post_request=request,
            slide_post_replay_only=True,
        )
    except IntegrityError as exc:
        if (
            getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            != "pk_slide_post_generation_receipts"
        ):
            raise
        # Re-enter the same authenticated lock path with lookup-only semantics.
        # No receipt may become a fresh dispatch after a collision.
        return dispatch_item_render_for(
            str(request.item_id),
            ownership_epoch,
            expected_slide_post_version=request.expected_version,
            slide_post_request=request,
            slide_post_replay_only=True,
        )
