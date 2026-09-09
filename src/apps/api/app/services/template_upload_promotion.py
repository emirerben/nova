"""Crash-recoverable promotion of staged template-job uploads.

The API commits a Job plus this immutable journal before copying any object.
Copies use deterministic destinations and generation preconditions, so a lost
response or process death can be resumed safely by the periodic reconciler.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app import storage
from app.database import sync_session
from app.models import Job

TEMPLATE_UPLOAD_PROMOTION_FIELD = "_template_upload_promotion"
TEMPLATE_UPLOAD_PROMOTION_VERSION = 1
TEMPLATE_UPLOAD_PROMOTION_MAX_AGE = timedelta(hours=23)


@dataclass(frozen=True, slots=True)
class TemplateUploadPromotionResult:
    state: str
    job_id: uuid.UUID
    promoted_paths: tuple[str, ...] = ()


def build_template_upload_promotion(
    paths: list[str],
    *,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    source_generations: dict[str, str],
) -> dict[str, Any]:
    """Build the durable, server-authored journal stored with the Job."""

    expected_prefix = f"staging/{user_id}/"
    clips: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        if not path.startswith("staging/"):
            clips.append(
                {
                    "source_path": path,
                    "source_generation": None,
                    "destination_path": path,
                    "staged": False,
                }
            )
            continue
        if not path.startswith(expected_prefix):
            raise ValueError("staged upload owner mismatch")
        generation = source_generations.get(path)
        if generation is None:
            raise ValueError("staged upload generation was not captured")
        extension = os.path.splitext(path)[1].lower()
        extension = extension if extension in {".mp4", ".mov"} else ".mp4"
        clips.append(
            {
                "source_path": path,
                "source_generation": str(generation),
                "destination_path": (
                    f"users/{user_id}/jobs/{job_id}/source/clip_{index:03d}{extension}"
                ),
                "staged": True,
            }
        )
    return {
        "version": TEMPLATE_UPLOAD_PROMOTION_VERSION,
        "owner_id": str(user_id),
        "clips": clips,
    }


def _validated_clips(job: Job) -> list[dict[str, Any]]:
    plan = job.assembly_plan if isinstance(job.assembly_plan, dict) else {}
    journal = plan.get(TEMPLATE_UPLOAD_PROMOTION_FIELD)
    if not isinstance(journal, dict) or journal.get("version") != TEMPLATE_UPLOAD_PROMOTION_VERSION:
        raise ValueError("template upload promotion journal is missing or unsupported")
    if journal.get("owner_id") != str(job.user_id):
        raise ValueError("template upload promotion owner changed")
    raw_clips = journal.get("clips")
    if not isinstance(raw_clips, list) or not raw_clips:
        raise ValueError("template upload promotion has no clips")

    expected_source_prefix = f"staging/{job.user_id}/"
    expected_destination_prefix = f"users/{job.user_id}/jobs/{job.id}/source/"
    clips: list[dict[str, Any]] = []
    for raw in raw_clips:
        if not isinstance(raw, dict):
            raise ValueError("template upload promotion clip is malformed")
        source = raw.get("source_path")
        destination = raw.get("destination_path")
        staged = raw.get("staged") is True
        generation = raw.get("source_generation")
        if not isinstance(source, str) or not isinstance(destination, str):
            raise ValueError("template upload promotion paths are malformed")
        if staged:
            if not source.startswith(expected_source_prefix):
                raise ValueError("template upload promotion source owner mismatch")
            if not destination.startswith(expected_destination_prefix):
                raise ValueError("template upload promotion destination owner mismatch")
            if not isinstance(generation, str) or not generation:
                raise ValueError("template upload promotion generation is missing")
        elif destination != source or generation is not None:
            raise ValueError("non-staged template clip journal is malformed")
        clips.append(raw)
    return clips


def resume_template_upload_promotion(job_id: str | uuid.UUID) -> TemplateUploadPromotionResult:
    """Resume one journal under the Job row lock and finalize it atomically.

    The database lock intentionally spans server-side GCS copies. Only one
    bounded job is reconciled at a time, and holding the lock prevents the API
    and Beat from both dispatching the same newly promoted Job.
    """

    parsed_job_id = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
    staged_sources: list[tuple[str, str]] = []
    promoted_paths: list[str] = []

    with sync_session() as db:
        job = (
            db.execute(select(Job).where(Job.id == parsed_job_id).with_for_update(skip_locked=True))
            .scalars()
            .one_or_none()
        )
        if job is None:
            return TemplateUploadPromotionResult("busy_or_missing", parsed_job_id)
        if job.status == "queued" and not (
            isinstance(job.assembly_plan, dict)
            and TEMPLATE_UPLOAD_PROMOTION_FIELD in job.assembly_plan
        ):
            paths = tuple((job.all_candidates or {}).get("clip_paths") or ())
            return TemplateUploadPromotionResult("ready", parsed_job_id, paths)
        if job.job_type != "template" or job.status != "importing":
            return TemplateUploadPromotionResult("ignored", parsed_job_id)

        clips = _validated_clips(job)
        for clip in clips:
            source = str(clip["source_path"])
            destination = str(clip["destination_path"])
            if clip["staged"]:
                generation = str(clip["source_generation"])
                # copy_object_generation is idempotent for this deterministic,
                # server-only destination: an existing object means a prior
                # attempt copied it but lost the response/DB commit.
                storage.copy_object_generation(
                    source,
                    destination,
                    source_generation=generation,
                )
                staged_sources.append((source, generation))
            promoted_paths.append(destination)

        candidates = dict(job.all_candidates or {})
        candidates["clip_paths"] = promoted_paths
        plan = dict(job.assembly_plan or {})
        plan.pop(TEMPLATE_UPLOAD_PROMOTION_FIELD, None)
        job.raw_storage_path = promoted_paths[0]
        job.all_candidates = candidates
        job.assembly_plan = plan or None
        job.status = "queued"
        db.commit()

    # The committed Job now references only durable destinations. Source
    # deletion is best-effort; the bucket's one-day staging lifecycle is the
    # backstop if this process dies during cleanup.
    for source, generation in staged_sources:
        storage.delete_object_generation_best_effort(source, generation=generation)

    return TemplateUploadPromotionResult("promoted", parsed_job_id, tuple(promoted_paths))


def record_template_upload_promotion_failure(
    job_id: str | uuid.UUID,
    *,
    error_type: str,
    now: datetime | None = None,
) -> str:
    """Persist retry evidence and terminalize before staging lifecycle expiry."""

    parsed_job_id = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
    current = now or datetime.now(UTC)
    with sync_session() as db:
        job = (
            db.execute(select(Job).where(Job.id == parsed_job_id).with_for_update())
            .scalars()
            .one_or_none()
        )
        if job is None or job.status != "importing":
            return "ignored"
        plan = dict(job.assembly_plan or {})
        journal = plan.get(TEMPLATE_UPLOAD_PROMOTION_FIELD)
        if not isinstance(journal, dict):
            return "ignored"
        journal = dict(journal)
        journal["attempts"] = int(journal.get("attempts") or 0) + 1
        journal["last_error_type"] = str(error_type)[:120]
        journal["last_attempt_at"] = current.isoformat()
        plan[TEMPLATE_UPLOAD_PROMOTION_FIELD] = journal
        job.assembly_plan = plan
        if job.created_at <= current - TEMPLATE_UPLOAD_PROMOTION_MAX_AGE:
            job.status = "processing_failed"
            job.failure_reason = "upload_promotion_failed"
            job.error_detail = "The staged upload could not be attached before it expired"
            state = "terminal"
        else:
            state = "retrying"
        db.commit()
        return state
