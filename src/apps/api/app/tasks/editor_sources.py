"""Background preparation for phone-editor source imports."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm.attributes import flag_modified

from app import storage
from app.database import sync_session
from app.kria.media_sources import MediaUploadContract
from app.models import CreationThread, CreationThreadUploadReservation, Job, PlanItem, PlanItemAsset
from app.pipeline.probe import ProbeError, probe_video
from app.schemas.edit_proposal import MAX_EDIT_PROPOSAL_MEDIA
from app.services.phone_editor_sources import (
    EDITOR_SOURCES_FIELD,
    attempt_matches,
    canonical_source,
    merge_editor_sources,
)
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD, PhoneSourceBinding
from app.services.phone_visuals import bind_phone_visual_assets, phone_composable_video
from app.worker import celery_app


def _variant(job: Job, variant_id: str) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in (job.assembly_plan or {}).get("variants") or []
            if isinstance(row, dict) and row.get("variant_id") == variant_id
        ),
        None,
    )


def _registry(variant: dict[str, Any]) -> dict[str, Any]:
    raw = variant.get(EDITOR_SOURCES_FIELD)
    return copy.deepcopy(raw) if isinstance(raw, dict) else {"imports": {}, "sources": []}


def _guard_matches(job: Job, variant: dict[str, Any], record: dict[str, Any]) -> bool:
    from app.routes.generative_jobs import (
        _guided_v2_revision,
        _phone_editor_media_available,
        variant_render_baseline,
    )

    revision = _guided_v2_revision(job, variant) or {}
    return (
        _phone_editor_media_available(job, variant)
        and str(job.user_id) == record["user_id"]
        and record.get("base_generation") == variant_render_baseline(variant)
        and record.get("guided_revision_number") == revision.get("revision_number")
    )


def _fail(
    job_id: str, variant_id: str, import_id: str, attempt_id: str, *, code: str, retryable: bool
) -> None:
    with sync_session() as db:
        job = db.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one_or_none()
        if job is None or (variant := _variant(job, variant_id)) is None:
            return
        registry = _registry(variant)
        record = registry["imports"].get(import_id)
        if not attempt_matches(record, attempt_id):
            return
        record.update(
            {"status": "failed", "error": code, "reason_code": code, "retryable": retryable}
        )
        registry["imports"][import_id] = record
        variant[EDITOR_SOURCES_FIELD] = registry
        flag_modified(job, "assembly_plan")
        db.commit()


def _prepare_footage(record: dict[str, Any]) -> tuple[dict[str, Any], PhoneSourceBinding]:
    with sync_session() as db:
        reservation = db.execute(
            select(CreationThreadUploadReservation).where(
                CreationThreadUploadReservation.id == record["reservation_id"]
            )
        ).scalar_one_or_none()
        if reservation is None or str(reservation.creator_id) != record["user_id"]:
            raise AdmissionError("source_reservation_missing")
        contract = MediaUploadContract.model_validate(reservation.upload_contract or {})
        if (
            contract.purpose != "analysis_proxy"
            or contract.proxy is None
            or contract.proxy.original.kind != "video"
        ):
            raise AdmissionError("source_reservation_invalid")
        path, media_id = reservation.object_path, reservation.media_id
    try:
        metadata = storage.object_metadata(path)
        generation = str(metadata.generation or "")
        if not generation or metadata.size <= 0:
            raise AdmissionError("source_upload_unavailable", retryable=True)
        url = storage.signed_get_url_for_generation(path, generation=generation)
        probe = probe_video(url)
    except (FileNotFoundError, ProbeError) as exc:
        raise AdmissionError("source_upload_unavailable", retryable=True) from exc
    if not phone_composable_video(probe.codec, probe.pix_fmt):
        raise AdmissionError("source_codec_unsupported")
    if (
        probe.width != contract.proxy.width
        or probe.height != contract.proxy.height
        or abs(probe.fps - contract.proxy.frame_rate) > 0.01
        or probe.rotation_degrees != 0
    ):
        raise AdmissionError("source_proxy_geometry_invalid")
    try:
        contract.proxy.verify_registered(float(probe.duration_s), bool(probe.has_audio))
    except ValueError as exc:
        raise AdmissionError("source_proxy_metadata_invalid") from exc
    binding = PhoneSourceBinding(
        media_id=media_id,
        proxy_path=path,
        generation=generation,
        original=contract.proxy.original,
    )
    return (
        {
            "media_id": media_id,
            "lane": "clip",
            "gcs_path": path,
            "generation": generation,
            "kind": "video",
            "duration_s": float(probe.duration_s),
        },
        binding,
    )


def _prepare_visual(job_id: str, record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    with sync_session() as db:
        asset = db.get(PlanItemAsset, record["source_id"])
        job = db.get(Job, job_id)
        if asset is None or job is None or str(asset.user_id) != record["user_id"]:
            raise AdmissionError("visual_not_found")
        if asset.plan_item_id != job.content_plan_item_id or asset.status != "ready":
            raise AdmissionError("visual_not_ready")
        pin = {str(asset.id): (asset.kind, asset.gcs_path, str(asset.gcs_generation or ""))}
    binding = bind_phone_visual_assets(sync_session, job_id=job_id, pins=pin)[0]
    return (
        {
            "media_id": binding.media_id,
            "lane": "asset",
            "gcs_path": binding.gcs_path,
            "generation": binding.generation,
            "kind": binding.kind,
            "duration_s": binding.duration_s if binding.kind == "video" else None,
        },
        binding.model_dump(mode="json"),
    )


class AdmissionError(ValueError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def reservation_lock_busy(exc: DBAPIError) -> bool:
    """Recognize PostgreSQL NOWAIT contention across psycopg/asyncpg adapters."""
    return (getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)) == "55P03"


def _recheck_source(
    db: Any,
    job: Job,
    record: dict,
    source: dict,
    receipt: dict,
    *,
    visual_asset: PlanItemAsset | None = None,
) -> CreationThreadUploadReservation | None:
    """Re-read DB identity under the job lock after slow preparation completed."""
    if record["source_kind"] == "footage":
        try:
            reservation = db.execute(
                select(CreationThreadUploadReservation)
                .where(CreationThreadUploadReservation.id == record["reservation_id"])
                .with_for_update(nowait=True)
            ).scalar_one_or_none()
        except DBAPIError as exc:
            if not reservation_lock_busy(exc):
                raise
            # The enclosing session must exit before _fail opens another
            # transaction; PostgreSQL marks this transaction failed on NOWAIT.
            raise AdmissionError("source_reservation_busy", retryable=True) from exc
        if reservation is None:
            raise AdmissionError("source_reservation_missing")
        thread = db.get(CreationThread, reservation.thread_id)
        if (
            thread is None
            or thread.creator_id != job.user_id
            or thread.active_plan_item_id != job.content_plan_item_id
            or reservation.creator_id != job.user_id
            or reservation.media_id != source["media_id"]
            or source["media_id"] != record["source_id"]
            or reservation.object_path != source["gcs_path"]
        ):
            raise AdmissionError("source_reservation_invalid")
        contract = MediaUploadContract.model_validate(reservation.upload_contract or {})
        if (
            contract.proxy is None
            or contract.proxy.original.model_dump(mode="json") != receipt["original"]
        ):
            raise AdmissionError("source_reservation_changed")
        return reservation
    else:
        asset = visual_asset
        if (
            asset is None
            or record["source_kind"] != "visual"
            or str(asset.id) != source["media_id"]
            or record["source_id"] != source["media_id"]
            or asset.user_id != job.user_id
            or asset.plan_item_id != job.content_plan_item_id
            or asset.status != "ready"
            or asset.kind != source["kind"]
            or asset.gcs_path != source["gcs_path"]
            or str(asset.gcs_generation or "") != source["generation"]
        ):
            raise AdmissionError("visual_changed")
    return None


def _admit(
    registry: dict,
    *,
    catalog: list[dict],
    source: dict,
    receipt_key: str,
    receipt: dict,
    assembly: dict,
) -> tuple[int, dict]:
    source = canonical_source(source)
    existing = next((row for row in catalog if row["media_id"] == source["media_id"]), None)
    if existing is not None:
        if canonical_source(existing) != source:
            raise AdmissionError("editor_source_identity_conflict")
        index = next(
            index for index, row in enumerate(catalog) if row["media_id"] == source["media_id"]
        )
    else:
        if len(catalog) >= MAX_EDIT_PROPOSAL_MEDIA:
            raise AdmissionError("editor_source_limit")
        index = len(catalog)
    prior = next(
        (row for row in registry["sources"] if row["media_id"] == source["media_id"]), None
    )
    if prior is not None:
        if prior.get(receipt_key) != receipt or prior["source_index"] != index:
            raise AdmissionError("editor_source_identity_conflict")
        return index, source
    # Reuse worker-pinned receipts byte-for-byte for an approved source.
    field = PHONE_SOURCES_FIELD if receipt_key == "source_binding" else PHONE_VISUALS_FIELD
    pinned = next(
        (row for row in assembly.get(field, []) if row.get("media_id") == source["media_id"]), None
    )
    if pinned is not None:
        if pinned != receipt:
            raise AdmissionError("editor_source_identity_conflict")
        # Already bound approved sources need only an import-result record.
        # Duplicating their receipt would duplicate compiler asset identities.
        return index, source
    registry["sources"].append(
        {**source, "source_index": index, "status": "ready", receipt_key: receipt}
    )
    merge_editor_sources(catalog, {EDITOR_SOURCES_FIELD: registry})
    return index, source


@celery_app.task(
    name="tasks.prepare_phone_editor_source",
    bind=True,
    max_retries=0,
    soft_time_limit=300,
    time_limit=360,
)
def prepare_phone_editor_source(
    self: Any, job_id: str, variant_id: str, import_id: str, attempt_id: str
) -> None:
    """Slow preparation is outside locks; attempt tokens fence stale workers."""
    from app.routes.generative_jobs import _guided_v2_revision

    with sync_session() as db:
        job = db.get(Job, job_id)
        variant = _variant(job, variant_id) if job else None
        record = _registry(variant)["imports"].get(import_id) if variant else None
        if not attempt_matches(record, attempt_id):
            return
        record = copy.deepcopy(record)
    try:
        if record["source_kind"] == "footage":
            source, binding = _prepare_footage(record)
            receipt_key, receipt = "source_binding", binding.model_dump(mode="json")
        else:
            source, receipt = _prepare_visual(job_id, record)
            receipt_key = "visual_binding"
        source = canonical_source(source)
        # Storage I/O remains outside the transaction. The exact-generation
        # receipt remains authoritative even if an overwrite follows this read.
        metadata = storage.object_metadata(source["gcs_path"])
        if str(metadata.generation or "") != source["generation"]:
            raise AdmissionError("source_generation_stale")
        with sync_session() as db:
            # Canonical lock order: item, visual asset, then job. The copied
            # import identifies the asset; recheck its identity under the locks.
            item = db.get(
                PlanItem, job.content_plan_item_id, with_for_update=True, populate_existing=True
            )
            visual_asset = (
                db.get(
                    PlanItemAsset,
                    record["source_id"],
                    with_for_update=True,
                    populate_existing=True,
                )
                if record["source_kind"] == "visual"
                else None
            )
            job = db.execute(
                select(Job).where(Job.id == job_id).with_for_update()
            ).scalar_one_or_none()
            if job is None or (variant := _variant(job, variant_id)) is None:
                return
            registry = _registry(variant)
            current = registry["imports"].get(import_id)
            if not attempt_matches(current, attempt_id):
                return
            if item is None or item.current_job_id != job.id or item.id != job.content_plan_item_id:
                raise AdmissionError("source_revision_stale")
            if not _guard_matches(job, variant, current):
                raise AdmissionError("source_revision_stale")
            if current["source_kind"] != record["source_kind"]:
                raise AdmissionError("source_revision_stale")
            reservation = _recheck_source(
                db, job, current, source, receipt, visual_asset=visual_asset
            )
            catalog = (_guided_v2_revision(job, variant) or {})["sources"]
            index, source = _admit(
                registry,
                catalog=catalog,
                source=source,
                receipt_key=receipt_key,
                receipt=receipt,
                assembly=job.assembly_plan,
            )
            if reservation is not None:
                # Admission consumes this upload just like project attachment.
                # Retain the row for another already-preparing import, but no
                # longer block project deletion for the 15-minute PUT lease.
                reservation.expires_at = min(reservation.expires_at, datetime.now(UTC))
            current.update(
                status="ready",
                source_index=index,
                source=source,
                error=None,
                reason_code=None,
                retryable=False,
            )
            registry["imports"][import_id] = current
            variant[EDITOR_SOURCES_FIELD] = registry
            flag_modified(job, "assembly_plan")
            db.commit()
    except AdmissionError as exc:
        _fail(job_id, variant_id, import_id, attempt_id, code=exc.code, retryable=exc.retryable)
    except DBAPIError:
        # Do not mislabel unrelated database faults as source contention. The
        # durable attempt lease makes an interrupted completion recoverable.
        raise
    except ValueError:
        _fail(
            job_id,
            variant_id,
            import_id,
            attempt_id,
            code="source_validation_failed",
            retryable=False,
        )
    except Exception:
        _fail(
            job_id, variant_id, import_id, attempt_id, code="source_prepare_failed", retryable=True
        )
