"""Owned, opt-in generated assets with durable results and no automatic regeneration."""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from google.api_core.exceptions import GoogleAPIError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import storage
from app.agents._runtime import RunContext
from app.config import settings
from app.db_locks import acquire_locked_rows_sync
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    GeneratedMediaOperation,
    MediaAssetSource,
    Persona,
    PlanItem,
    PlanItemAsset,
)
from app.services.ai_cost_control import (
    PaidCallRequest,
    mark_paid_call_started,
    mark_paid_call_unknown,
    release_paid_call,
    reserve_paid_call,
    settle_paid_call_cost,
)
from app.services.generated_asset_contracts import (
    IMAGE_MODEL,
    MAX_IMAGE_BYTES,
    NARRATION_MODEL,
    ImageRequest,
    NarrationRequest,
    image_payload,
    narration_events,
)
from app.services.generated_media_admission import (
    admit_generated_asset,
    cleanup_expired_generated_media,
    persist_generated_asset,
)
from app.services.generated_media_transport import (
    GENERATED_PRICE_VERSION,
    OpenAIGeneratedTransport,
    ProviderRejected,
    generated_usage_cost,
    validate_reference,
)


class GeneratedMediaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_event_id: uuid.UUID
    capability: Literal["image", "image_edit", "narration"]
    prompt: str = Field(min_length=1, max_length=1500)
    editable_title: str = Field(default="", max_length=200)
    transparent_background: bool = False
    source_asset_id: uuid.UUID | None = None
    source_generation: str | None = Field(default=None, max_length=100)
    script_id: str | None = Field(default=None, max_length=200)
    script_version: int | None = Field(default=None, ge=1)
    approved_script_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def capability_inputs(self):
        if self.transparent_background and self.capability != "image":
            raise ValueError("Transparent backgrounds require image generation")
        source = self.source_asset_id is not None or self.source_generation is not None
        script = (
            self.script_id is not None
            or self.script_version is not None
            or self.approved_script_sha256 is not None
        )
        if self.capability == "image" and (source or script):
            raise ValueError("Image generation does not accept source or script fields")
        if self.capability == "image_edit" and (
            not self.source_asset_id or not self.source_generation or script
        ):
            raise ValueError("Image editing requires only a pinned image source")
        if self.capability == "narration" and (
            source
            or self.editable_title
            or not self.script_id
            or not self.script_version
            or not self.approved_script_sha256
        ):
            raise ValueError("Narration requires only the approved script identity")
        return self


class GeneratedMediaAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script_id: str | None = None
    script_version: int | None = None
    approved_script_sha256: str | None = None


def generated_media_enabled(capability: str) -> bool:
    try:
        verified = date.fromisoformat(settings.generated_media_price_verified_at)
    except ValueError:
        return False
    return bool(
        settings.ai_cost_control_enabled
        and settings.generated_media_live_approved
        and settings.generated_media_billing_organization
        and settings.openai_api_key
        and timedelta(0) <= date.today() - verified <= timedelta(days=7)
        and settings.generated_media_max_request_usd >= (0.1 if capability == "narration" else 2)
        and settings.ai_openai_monthly_budget_usd > 0
        and (
            settings.generated_narration_enabled and settings.ai_openai_narration_budget_usd > 0
            if capability == "narration"
            else settings.generated_images_enabled and settings.ai_openai_image_budget_usd > 0
        )
    )


def owned_item(db, owner_id, item_id, *, lock=False):
    item = db.get(PlanItem, item_id)
    if item is None:
        raise ValueError("Plan item not found")
    plan = db.get(ContentPlan, item.content_plan_id)
    if plan is None or plan.user_id != owner_id:
        raise ValueError("Plan item not found")
    persona = db.get(Persona, plan.persona_id)
    if persona is None or persona.user_id != owner_id:
        raise ValueError("Plan item not found")
    if lock:
        plan_id, persona_id = plan.id, persona.id
        rows = acquire_locked_rows_sync(
            db,
            {Persona: persona_id, ContentPlan: plan_id, PlanItem: item_id},
            populate_existing=True,
        )
        persona, plan, item = rows[Persona], rows[ContentPlan], rows[PlanItem]
        if (
            persona is None
            or persona.user_id != owner_id
            or plan is None
            or plan.user_id != owner_id
            or plan.persona_id != persona_id
            or item is None
            or item.content_plan_id != plan_id
        ):
            raise ValueError("Plan item not found")
    return item


def narration_script(db, owner_id, item_id, *, lock=False):
    item = owned_item(db, owner_id, item_id)
    threads = select(CreationThread.id).where(
        CreationThread.creator_id == owner_id,
        CreationThread.active_plan_item_id == item_id,
        CreationThread.status == "active",
    )
    if lock:
        threads = threads.with_for_update()
    ids = list(db.scalars(threads))
    rows = (
        db.scalars(
            select(CreationThreadEvent.payload)
            .where(
                CreationThreadEvent.thread_id.in_(ids),
                CreationThreadEvent.event_type == "assistant_response",
                CreationThreadEvent.role == "assistant",
            )
            .order_by(CreationThreadEvent.created_at.desc(), CreationThreadEvent.sequence.desc())
            .limit(200)
        )
        if ids
        else []
    )
    script = None
    for payload in rows:
        candidate = (payload or {}).get("voiceover_script")
        if (
            isinstance(candidate, dict)
            and candidate.get("status") == "draft"
            and candidate.get("text")
            and candidate.get("item_id") == str(item_id)
        ):
            script = {
                "script_id": candidate["script_id"],
                "script_version": candidate["revision"],
                "text": candidate["text"],
            }
            break
    if (
        script is None
        and isinstance(item.voiceover_script, dict)
        and item.voiceover_script.get("text")
    ):
        script = {
            "script_id": f"item:{item_id}",
            "script_version": item.voiceover_script["version"],
            "text": item.voiceover_script["text"],
        }
    if script is None:
        raise ValueError("Approved script is unavailable")
    script["approved_script_sha256"] = hashlib.sha256(script["text"].encode()).hexdigest()
    return script


def _script_matches(actual, expected):
    if any(
        actual.get(k) != expected.get(k)
        for k in ("script_id", "script_version", "approved_script_sha256")
    ):
        raise ValueError("Script changed; review the current script")


def _reference(db, owner_id, item_id, body):
    asset = db.get(PlanItemAsset, body.source_asset_id) if body.source_asset_id else None
    prefix = f"users/{owner_id}/plan/{item_id}/pool/"
    if (
        asset is None
        or asset.user_id != owner_id
        or asset.plan_item_id != item_id
        or asset.kind != "image"
        or asset.status != "ready"
        or str(asset.gcs_generation) != body.source_generation
    ):
        raise ValueError("Image source changed or is unavailable")
    expected_identity = None
    if asset.library_source_id is not None:
        source = db.get(MediaAssetSource, asset.library_source_id)
        if (
            source is None
            or source.user_id != owner_id
            or source.asset_id != asset.library_asset_id
            or source.role != "original"
            or source.status != "available"
            or source.object_path != asset.gcs_path
            or source.object_generation != body.source_generation
        ):
            raise ValueError("Image source changed or is unavailable")
        expected_identity = (source.uploaded_sha256, source.byte_count)
    else:
        from app.services.phone_visuals import _owned_pool_path

        if not _owned_pool_path(asset.gcs_path, prefix):
            raise ValueError("Image source changed or is unavailable")
    metadata = storage.object_metadata(asset.gcs_path)
    if (
        str(metadata.generation) != body.source_generation
        or not 0 < metadata.size <= MAX_IMAGE_BYTES
    ):
        raise ValueError("Image source changed or is unavailable")
    with tempfile.TemporaryDirectory(prefix="kria-image-reference-") as directory:
        local = Path(directory) / "reference"
        storage.download_generation_to_file(
            asset.gcs_path, str(local), generation=body.source_generation
        )
        if local.stat().st_size != metadata.size:
            raise ValueError("Image source bytes changed")
        content = local.read_bytes()
    if len(content) != metadata.size:
        raise ValueError("Image source bytes changed")
    if expected_identity is not None and expected_identity != (
        hashlib.sha256(content).hexdigest(),
        len(content),
    ):
        raise ValueError("Library original bytes do not match their verified identity")
    validate_reference(content)
    return content


def get_generated_operation(db, operation_id, *, owner_id, item_id, lock=False):
    owned_item(db, owner_id, item_id)
    operation = db.get(
        GeneratedMediaOperation, operation_id, with_for_update=lock, populate_existing=lock
    )
    if operation is None or operation.user_id != owner_id or operation.plan_item_id != item_id:
        raise ValueError("Generated asset not found")
    return operation


def retry_generated_storage(db, operation_id, *, owner_id, item_id):
    operation = get_generated_operation(
        db, operation_id, owner_id=owner_id, item_id=item_id, lock=True
    )
    try:
        admit_generated_asset(db, operation, owner_id=owner_id, plan_item_id=item_id)
    except (OSError, ValueError, SQLAlchemyError, GoogleAPIError):
        db.rollback()
        operation = get_generated_operation(
            db, operation_id, owner_id=owner_id, item_id=item_id, lock=True
        )
        operation.failure_code = "storage_failed"
    db.commit()
    return operation


def execute_generated_media(
    db: Session, *, owner_id, item_id, body: GeneratedMediaRequest, ctx: RunContext, transport=None
):
    if not generated_media_enabled(body.capability):
        raise ValueError("Generated media is unavailable")
    owned_item(db, owner_id, item_id)
    request = (
        NarrationRequest(body.prompt)
        if body.capability == "narration"
        else ImageRequest(
            body.prompt, body.editable_title, transparent_background=body.transparent_background
        )
    )
    if body.capability == "narration":
        narration_events(request)
        script = narration_script(db, owner_id, item_id)
        _script_matches(script, body.model_dump())
        if body.prompt != script["text"]:
            raise ValueError("Script changed; review the current script")
    else:
        image_payload(request)
    digest_payload = body.model_dump(mode="json")
    if not body.transparent_background:
        digest_payload.pop("transparent_background")
    digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True).encode()).hexdigest()
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": f"generated:{owner_id}:{item_id}:{body.client_event_id}"},
    )
    existing = db.scalar(
        select(GeneratedMediaOperation).where(
            GeneratedMediaOperation.user_id == owner_id,
            GeneratedMediaOperation.plan_item_id == item_id,
            GeneratedMediaOperation.client_event_id == str(body.client_event_id),
        )
    )
    if existing:
        if existing.request_digest != digest:
            raise ValueError("Idempotency key reused with different input")
        db.commit()
        return existing
    reference = _reference(db, owner_id, item_id, body) if body.capability == "image_edit" else None
    cleanup_expired_generated_media(db)
    operation = GeneratedMediaOperation(
        user_id=owner_id,
        plan_item_id=item_id,
        client_event_id=str(body.client_event_id),
        request_digest=digest,
        capability=body.capability,
        state="pending",
        request_metadata={
            k: v
            for k, v in body.model_dump(mode="json").items()
            if k not in {"prompt", "editable_title", "client_event_id", "capability"}
        },
    )
    db.add(operation)
    db.commit()
    feature = {
        "image": "generated_image",
        "image_edit": "generated_image_edit",
        "narration": "generated_narration",
    }[body.capability]
    model = NARRATION_MODEL if body.capability == "narration" else IMAGE_MODEL
    try:
        receipt = reserve_paid_call(
            PaidCallRequest(
                idempotency_key=f"generated:{operation.id}",
                feature=feature,
                model=model,
                estimated_cost_usd=0.1 if body.capability == "narration" else 2,
                ctx=ctx,
                provider="openai",
                capability="narration" if body.capability == "narration" else "image",
                price_version=GENERATED_PRICE_VERSION,
            )
        )
        if receipt is None:
            raise ValueError("Generated media requires an enforced ledger")
    except Exception:
        operation.state = "failed"
        operation.failure_code = "budget_unavailable"
        db.commit()
        raise
    operation.reservation_id = receipt.id
    operation.model = model
    operation.state = "submitting"
    db.commit()
    transport = transport or OpenAIGeneratedTransport()
    try:
        mark_paid_call_started(receipt)
        asset = (
            transport.narrate(request)
            if body.capability == "narration"
            else transport.edit(request, reference)
            if reference is not None
            else transport.generate(request)
        )
        cost = generated_usage_cost(asset.usage, body.capability)
        operation = get_generated_operation(
            db, operation.id, owner_id=owner_id, item_id=item_id, lock=True
        )
        persist_generated_asset(db, operation, asset, owner_id=owner_id, plan_item_id=item_id)
        db.commit()
        settle_paid_call_cost(
            receipt,
            cost_usd=cost,
            resolved_model=model,
            provider_request_id=asset.provider_request_id,
            usage_json=asset.usage,
        )
    except ProviderRejected as exc:
        release_paid_call(receipt)
        operation.state = "failed"
        operation.failure_code = "provider_rate_limited" if exc.retryable else "provider_rejected"
        db.commit()
        return operation
    except Exception:
        db.rollback()
        mark_paid_call_unknown(receipt)
        operation = get_generated_operation(
            db, operation.id, owner_id=owner_id, item_id=item_id, lock=True
        )
        operation.state = "unknown"
        operation.failure_code = "provider_outcome_unknown"
        db.commit()
        return operation
    return retry_generated_storage(db, operation.id, owner_id=owner_id, item_id=item_id)


def accept_generated_media(db, operation_id, *, owner_id, item_id, body: GeneratedMediaAccept):
    operation = get_generated_operation(db, operation_id, owner_id=owner_id, item_id=item_id)
    if operation.state != "ready":
        raise ValueError("Generated asset is not ready")
    if operation.capability == "narration":
        from app.services.phone_voiceover import inspect_voiceover_asset

        path, generation, digest = (
            operation.output_path,
            operation.output_generation,
            operation.content_sha256,
        )
        db.rollback()
        receipt = inspect_voiceover_asset(
            path, asset_id=str(operation_id), plan_item_id=str(item_id)
        )
        if receipt.generation != generation or receipt.fingerprint.sha256 != digest:
            raise ValueError("Generated narration changed")
    item = owned_item(db, owner_id, item_id, lock=True)
    operation = get_generated_operation(
        db, operation_id, owner_id=owner_id, item_id=item_id, lock=True
    )
    if operation.state != "ready":
        raise ValueError("Generated asset is not ready")
    if operation.capability != "narration":
        if operation.capability == "image_edit":
            source = db.get(PlanItemAsset, uuid.UUID(operation.request_metadata["source_asset_id"]))
            if (
                source is None
                or source.user_id != owner_id
                or source.plan_item_id != item_id
                or str(source.gcs_generation) != operation.request_metadata["source_generation"]
            ):
                raise ValueError("Image source changed")
        asset = db.get(PlanItemAsset, operation.asset_id)
        if (
            asset is None
            or asset.status != "ready"
            or str(asset.gcs_generation) != operation.output_generation
        ):
            raise ValueError("Generated image changed")
        operation.request_metadata = {**operation.request_metadata, "accepted": True}
        db.commit()
        return operation
    from app.services.plan_item_media import (
        current_detector_policy,
        mutate_plan_item_media,
        publish_preflight_after_commit,
    )
    from app.services.speech_cleanup_preflight import (
        mutation_current_analysis_sync,
        schedule_item_preflight_sync,
    )

    current = mutation_current_analysis_sync(db, item.id, for_update=True)
    script = narration_script(db, owner_id, item_id, lock=True)
    _script_matches(script, operation.request_metadata)
    _script_matches(script, body.model_dump())
    current_matches = (
        item.voiceover_gcs_path == operation.output_path
        and str(item.voiceover_generation) == operation.output_generation
    )
    if operation.request_metadata.get("accepted") and not current_matches:
        raise ValueError("Voiceover changed after acceptance")
    if current_matches:
        db.commit()
        return operation
    mutate_plan_item_media(
        item,
        detector_policy=current_detector_policy(),
        current_analysis=current,
        voiceover_gcs_path=operation.output_path,
        voiceover_generation=operation.output_generation,
        voiceover_duration_s=operation.duration_s,
        audio_mode="voiceover",
    )
    if script["script_id"].startswith("item:"):
        item.voiceover_script_recorded_version = script["script_version"]
    preflight = schedule_item_preflight_sync(db, item)
    operation.request_metadata = {**operation.request_metadata, "accepted": True}
    db.commit()
    publish_preflight_after_commit(preflight)
    return operation


def operation_response(operation):
    url = None
    if operation.state == "ready" and operation.output_path and operation.output_generation:
        url = storage.signed_get_url_for_generation(
            operation.output_path, generation=operation.output_generation
        )
    return {
        **{
            k: operation.request_metadata.get(k)
            for k in (
                "source_asset_id",
                "source_generation",
                "script_id",
                "script_version",
                "approved_script_sha256",
            )
        },
        "id": str(operation.id),
        "state": operation.state,
        "capability": operation.capability,
        "preview_url": url,
        "source_url": url,
        "asset_id": str(operation.asset_id) if operation.asset_id else None,
        "gcs_path": operation.output_path if url else None,
        "generation": operation.output_generation if url else None,
        "error_code": operation.failure_code,
        "retryable": operation.state == "provider_complete"
        and operation.failure_code == "storage_failed",
    }


def reconcile_generated_media(db):
    from sqlalchemy import update

    now = datetime.now(UTC)
    stale = db.execute(
        update(GeneratedMediaOperation)
        .where(
            GeneratedMediaOperation.state.in_(("pending", "submitting")),
            GeneratedMediaOperation.updated_at < now - timedelta(minutes=5),
        )
        .values(state="unknown", failure_code="provider_outcome_unknown")
    ).rowcount
    expired = cleanup_expired_generated_media(db, now=now)
    db.commit()
    return {"stale": stale, "expired": expired}
