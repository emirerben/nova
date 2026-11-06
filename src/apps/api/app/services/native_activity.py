"""Native terminal activity shares its writer's transaction and current target fences."""

import hashlib
import json
import uuid

from pydantic import ValidationError
from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import JSONB, UUID, insert

from app.kria.device_render import DeviceRenderIdentity
from app.kria.prepared_execution import variant_fence
from app.models import ContentPlan, CreationThread, Job, NativeActivity, Persona, PlanItem
from app.services.device_render import (
    checked_export_report,
    device_paused,
    device_record,
    device_status,
    variant_delivery_phase,
)


def _published_artifact_proven(job, variant_id, record, identity):
    attempt_id = str(record.get("published_attempt") or "")
    attempt = (record.get("attempts") or {}).get(attempt_id) or {}
    proof = attempt.get("publication_proof")
    if proof is None:
        # Historical reports qualify only with their full measured-byte evidence.
        if "actual_export_policy" not in record or not checked_export_report(
            record, device_status(job, variant_id), attempt_id
        ):
            return False
        proof = attempt.get("actual_export_verification") or {}
    elif not isinstance(proof, dict) or proof.get("version") != 1:
        return False
    policy = record.get("actual_export_policy")
    if policy is not None and (
        not isinstance(policy, dict)
        or policy.get("version") != 1
        or not policy.get("source_receipts_digest")
    ):
        return False
    return bool(
        attempt.get("path")
        and isinstance(attempt.get("size"), int)
        and attempt["size"] > 0
        and isinstance(attempt.get("sha256"), str)
        and len(attempt["sha256"]) == 64
        and proof.get("artifact_path") == attempt["path"]
        and proof.get("file_size_bytes") == attempt["size"]
        and proof.get("sha256") == attempt["sha256"]
        and proof.get("storage_generation")
        and proof.get("identity") == identity
        and proof.get("source_receipts_digest") == (policy or {}).get("source_receipts_digest")
        and any(
            row.get("variant_id") == variant_id
            and row.get("video_path") == attempt["path"]
            and str(row.get("render_generation_id")) == attempt_id
            for row in (job.assembly_plan or {}).get("variants") or []
            if isinstance(row, dict)
        )
    )


def terminal_snapshot(job, variant_id):
    if job.status == "cancelled":
        return None
    phase = variant_delivery_phase(job, variant_id)
    if phase not in {"published", "needs_attention"}:
        return None
    record = device_record(job, variant_id)
    if device_paused(record):
        return None
    fence = variant_fence(job.assembly_plan or {}, variant_id)
    if (fence is not None or record.get("prepared_execution") is not None) and record.get(
        "prepared_execution"
    ) != fence:
        return None
    try:
        identity = DeviceRenderIdentity.model_validate(
            record["status"]["request"]["identity"]
        ).model_dump(mode="json")
        if identity["job_id"] != str(job.id) or identity["variant_id"] != variant_id:
            return None
        if phase == "published" and (
            str(record.get("published_attempt") or "") != str(record.get("base_generation"))
            or not _published_artifact_proven(job, variant_id, record, identity)
        ):
            return None
    except (KeyError, TypeError, ValueError, ValidationError):
        return None
    return {
        "identity": identity,
        "generation": str(record["base_generation"]),
        "kind": "video_ready" if phase == "published" else "action_required",
    }


def owned_targets(owner_id):
    return (
        select(CreationThread.id, PlanItem.id.label("item_id"), Job.id.label("job_id"))
        .join(PlanItem, PlanItem.id == CreationThread.active_plan_item_id)
        .join(ContentPlan, ContentPlan.id == PlanItem.content_plan_id)
        .join(Persona, Persona.id == ContentPlan.persona_id)
        .join(Job, Job.id == PlanItem.current_job_id)
        .where(
            CreationThread.creator_id == owner_id,
            CreationThread.content_plan_id == ContentPlan.id,
            CreationThread.active_job_id == Job.id,
            Job.content_plan_item_id == PlanItem.id,
            Job.user_id == owner_id,
            ContentPlan.user_id == owner_id,
            Persona.user_id == owner_id,
            ContentPlan.ownership_quarantined_at.is_(None),
            func.coalesce(Job.content_plan_ownership_epoch, 0) == ContentPlan.ownership_epoch,
        )
    )


def activity_statement(job, variant_id, *, kind=None):
    snapshot = terminal_snapshot(job, variant_id)
    if (
        snapshot is None
        or (kind is not None and snapshot["kind"] != kind)
        or not getattr(job, "content_plan_item_id", None)
    ):
        return None
    epoch = int(getattr(job, "content_plan_ownership_epoch", None) or 0)
    semantic_key = hashlib.sha256(
        json.dumps({**snapshot, "owner": str(job.user_id), "epoch": epoch}, sort_keys=True).encode()
    ).hexdigest()
    targets = owned_targets(job.user_id).where(Job.id == job.id)
    values = targets.with_only_columns(
        literal(
            uuid.uuid5(uuid.NAMESPACE_URL, "kria-native-activity:" + semantic_key),
            type_=UUID(as_uuid=True),
        ),
        literal(job.user_id, type_=UUID(as_uuid=True)),
        CreationThread.id,
        PlanItem.id,
        Job.id,
        literal(variant_id),
        literal(epoch),
        literal(snapshot["identity"], type_=JSONB),
        literal(snapshot["generation"]),
        literal(snapshot["kind"]),
        literal(semantic_key),
    )
    return (
        insert(NativeActivity)
        .from_select(
            [
                "id",
                "owner_id",
                "thread_id",
                "item_id",
                "job_id",
                "variant_id",
                "ownership_epoch",
                "identity",
                "generation",
                "kind",
                "semantic_key",
            ],
            values,
        )
        .on_conflict_do_nothing(index_elements=[NativeActivity.semantic_key])
    )


async def record_native_activity(db, job, variant_id, *, kind="action_required"):
    statement = activity_statement(job, variant_id, kind=kind)
    if statement is not None:
        await db.execute(statement)


def activity_query(owner_id):
    targets = owned_targets(owner_id).subquery()
    return (
        select(NativeActivity, Job)
        .join(Job, Job.id == NativeActivity.job_id)
        .join(
            targets,
            (targets.c.id == NativeActivity.thread_id)
            & (targets.c.item_id == NativeActivity.item_id)
            & (targets.c.job_id == NativeActivity.job_id),
        )
        .join(PlanItem, PlanItem.id == NativeActivity.item_id)
        .join(ContentPlan, ContentPlan.id == PlanItem.content_plan_id)
        .where(
            NativeActivity.owner_id == owner_id,
            PlanItem.current_job_id == Job.id,
            Job.content_plan_item_id == PlanItem.id,
            NativeActivity.ownership_epoch == ContentPlan.ownership_epoch,
        )
    )


def is_current(activity, job):
    snapshot = terminal_snapshot(job, activity.variant_id)
    return snapshot is not None and all(
        snapshot[key] == getattr(activity, key) for key in ("identity", "generation", "kind")
    )
