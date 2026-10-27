"""Generation-bound human export review and private-safe daily projections.

No model calls. Object identity proves which bytes were inspected, not their
semantic correctness. Human viewing/listening and per-requirement verdicts are
recorded separately from the existing intent review.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app import storage
from app.models import CreationThread, RequestFollowingCase
from app.services.request_following_cases import digest, failed_pre_job_turns
from app.services.request_following_collection import eligible_case, source_capture

Category = Literal[
    "title",
    "labels",
    "captions",
    "order",
    "selection",
    "duration",
    "pacing",
    "readability",
    "audio_source",
    "audio_mix",
    "style",
    "preservation",
    "other",
]
Verdict = Literal["met", "partial", "missed", "unclear", "creator_changed"]
CATEGORIES = {
    "title": (
        "Requested title",
        "Verify the requested title, wording and placement reach the export.",
    ),
    "labels": ("Per-clip labels", "Verify requested labels appear on the correct clips."),
    "captions": ("Captions", "Verify requested captions, language and wording survive export."),
    "order": ("Clip order", "Compare the requested sequence with the exported sequence."),
    "selection": (
        "Included / excluded footage",
        "Verify all required footage is used and excluded footage stays out.",
    ),
    "duration": ("Video length", "Carry the agreed duration through planning and final export."),
    "pacing": ("Pacing", "Check requested pacing against the actual clip timings."),
    "readability": (
        "Text readability",
        "Check text size, contrast and time on screen in the export.",
    ),
    "audio_source": (
        "Requested voice / audio",
        "Trace the selected voice or audio source through the final mix.",
    ),
    "audio_mix": ("Audio mix", "Listen for missing, doubled or incorrectly mixed audio."),
    "style": (
        "Requested visual style",
        "Verify requested fonts, effects and styling in the export.",
    ),
    "preservation": (
        "Keep unrelated edits",
        "Check that the correction preserves previously accepted edits.",
    ),
    "other": (
        "Other instruction",
        "Open the private review and reproduce the confirmed missed instruction.",
    ),
}


class ArtifactIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=36, max_length=36)
    variant_id: str = Field(min_length=1, max_length=120)
    storage_generation: str = Field(min_length=1, max_length=100)
    byte_count: int = Field(gt=0)
    md5: str | None = None


class RequirementOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expectation_index: int = Field(ge=0, le=49)
    category: Category
    verdict: Verdict
    note: str = Field(default="", max_length=2000)
    time_s: float | None = Field(default=None, ge=0, le=86400)


class OutcomeReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact: ArtifactIdentity
    watched: bool = False
    listened: bool = False
    outcomes: list[RequirementOutcome] = Field(min_length=1, max_length=50)


def activity_at(case):
    values = [
        e.get("created_at", "")
        for e in (case.capture or {}).get("sections", {}).get("events", [])
        if e.get("kind") == "user_message" and e.get("actor") == "user" and e.get("content")
    ]
    return max(values, default="")


def delivery_counts(capture: dict) -> dict[str, int]:
    from app.kria.device_render import DeviceRenderStatus
    from app.services.device_render import DEVICE_RENDER_FIELD, checked_export_report

    sections = capture.get("sections") or {}
    jobs = {
        str(row.get("id") or key): row
        for key, row in sections.items()
        if key.startswith("job-") and isinstance(row, dict)
    }
    counts = dict(
        ready_jobs=0,
        failed_jobs=0,
        device_export_attempts=0,
        acknowledged_device_exports=0,
        verified_device_exports=0,
    )
    for job in jobs.values():
        counts["ready_jobs"] += int(job.get("status") in {"ready", "variants_ready"})
        counts["failed_jobs"] += int(job.get("status") in {"failed", "processing_failed"})
        assembly = job.get("assembly_plan") or {}
        variants = {
            v.get("variant_id"): v for v in assembly.get("variants") or [] if isinstance(v, dict)
        }
        for variant_id, record in (assembly.get(DEVICE_RENDER_FIELD) or {}).items():
            if not isinstance(record, dict):
                continue
            counts["device_export_attempts"] += len(record.get("attempts") or {})
            published = record.get("published_attempt")
            variant = variants.get(variant_id) or {}
            if not published or variant.get("render_generation_id") != published:
                continue
            try:
                status = DeviceRenderStatus.model_validate(record.get("status"))
            except (ValidationError, TypeError, ValueError):
                continue
            if status.phase != "published":
                continue
            counts["acknowledged_device_exports"] += 1
            if "actual_export_policy" in record and checked_export_report(
                record, status, published
            ):
                counts["verified_device_exports"] += 1
    pre_job_failures = failed_pre_job_turns(capture)
    return {
        **counts,
        "render_jobs": len(jobs),
        "pre_job_failure": int(bool(pre_job_failures)),
        "pre_job_failure_attempts": len(pre_job_failures),
    }


async def current_export(db, case, variant_id=None, *, sign=True):
    """Resolve only the current coherent owned job and an actual ready object."""
    from app.routes.creation_threads import _load_authorized_projection_rows
    from app.services.job_storage_paths import owned_job_output_path
    from app.services.public_assembly_plan import project_public_assembly_plan

    unavailable = {"status": "unavailable", "reason": "no_ready_current_export"}
    thread = await db.get(CreationThread, case.thread_id)
    if thread is None:
        return unavailable
    _, _, job, _ = await _load_authorized_projection_rows(db, thread, degrade=True)
    captured_job = (
        (case.capture or {}).get("sections", {}).get("integrity", {}).get("video") or {}
    ).get("job_id")
    if job is None or job.user_id != case.creator_id or str(job.id) != captured_job:
        return unavailable
    plan = project_public_assembly_plan(job.assembly_plan or {})
    variants = [
        v
        for v in plan.get("variants", [])
        if isinstance(v, dict)
        and v.get("render_status") == "ready"
        and v.get("editor_state") != "empty"
        and v.get("variant_id")
        and (variant_id is None or v.get("variant_id") == variant_id)
    ]
    for variant in variants:
        path = owned_job_output_path(variant.get("video_path"), job)
        if not path:
            continue
        try:
            meta = await run_in_threadpool(storage.object_metadata_once, path, timeout_s=5)
            if meta.size <= 0 or not meta.content_type.startswith("video/"):
                continue
            identity = ArtifactIdentity(
                job_id=str(job.id),
                variant_id=variant["variant_id"],
                storage_generation=meta.generation,
                byte_count=meta.size,
                md5=meta.md5_hash,
            )
            result = {"status": "available", "artifact": identity.model_dump()}
            if sign:
                result["url"] = await run_in_threadpool(
                    storage.signed_get_url_for_generation,
                    path,
                    generation=meta.generation,
                    expiration_minutes=15,
                )
            return result
        except (FileNotFoundError, storage.NotFound):
            continue
        except Exception:
            # No storage paths, signed URLs or credential errors in the response.
            raise HTTPException(503, "export_storage_unavailable") from None
    return unavailable


async def validate_outcome(db, case, body):
    value = body.outcome_review
    if value is None:
        return
    if body.decision != "approved" or not value.watched:
        raise HTTPException(422, "output_review_requires_approved_intent_and_viewing")
    indexes = [o.expectation_index for o in value.outcomes]
    if sorted(indexes) != list(range(len(body.expectations))):
        raise HTTPException(422, "one_verdict_per_requirement_required")
    if not value.listened and any(
        o.category in {"audio_source", "audio_mix", "captions"}
        and o.verdict not in {"unclear", "creator_changed"}
        for o in value.outcomes
    ):
        raise HTTPException(422, "audio_review_requires_listening")
    actual = await current_export(db, case, value.artifact.variant_id, sign=False)
    if actual.get("artifact") != value.artifact.model_dump():
        raise HTTPException(409, "export_changed_reload")


async def daily_report(db):
    """Fixed-vocabulary aggregate. Never serialize arbitrary review/capture text."""
    now = datetime.now(UTC)
    since = now - timedelta(days=14)
    rows = list(
        (
            await db.scalars(
                select(RequestFollowingCase)
                .where(
                    RequestFollowingCase.superseded.is_(False),
                    RequestFollowingCase.expires_at > now,
                    RequestFollowingCase.split == "development",
                    RequestFollowingCase.capture.is_not(None),
                )
                .order_by(RequestFollowingCase.created_at.desc())
                .limit(1001)
            )
        ).all()
    )
    counts = {
        "captured_sessions": 0,
        "request_sessions": 0,
        "recent_request_sessions": 0,
        "intent_reviewed_sessions": 0,
        "export_reviewed_sessions": 0,
        "stale_output_reviews": 0,
        "unavailable_output_reviews": 0,
        "invalid_output_reviews": 0,
    }
    requirements = dict(met=0, partial=0, missed=0, unclear=0, creator_changed=0, total=0)
    misses = defaultdict(set)
    findings = Counter()
    sessions = []
    delivery = dict(
        request_sessions=0,
        ready_job_sessions=0,
        pre_job_failure_sessions=0,
        render_jobs=0,
        failed_jobs=0,
        pre_job_failure_attempts=0,
        device_export_attempts=0,
        acknowledged_device_exports=0,
        verified_device_exports=0,
        fulfilled_reviewed_sessions=0,
        unfulfilled_reviewed_sessions=0,
        creator_changed_sessions=0,
    )
    for case in rows[:1000]:
        if not await eligible_case(db, case):
            continue
        counts["captured_sessions"] += 1
        activity = activity_at(case)
        if not activity:
            continue
        counts["request_sessions"] += 1
        try:
            at = datetime.fromisoformat(activity.replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            if at < since:
                continue
        except ValueError:
            continue
        counts["recent_request_sessions"] += 1
        observed = delivery_counts(case.capture or {})
        delivery["request_sessions"] += 1
        delivery["ready_job_sessions"] += int(observed["ready_jobs"] > 0)
        delivery["pre_job_failure_sessions"] += observed["pre_job_failure"]
        for key in (
            "render_jobs",
            "failed_jobs",
            "pre_job_failure_attempts",
            "device_export_attempts",
            "acknowledged_device_exports",
            "verified_device_exports",
        ):
            delivery[key] += observed[key]
        review = case.review or {}
        if review.get("decision") != "approved":
            continue
        thread = await db.get(CreationThread, case.thread_id)
        if not thread or digest(await source_capture(db, thread)) != case.source_sha256:
            if review.get("outcome_review"):
                counts["stale_output_reviews"] += 1
            continue
        counts["intent_reviewed_sessions"] += 1
        if not review.get("outcome_review"):
            continue
        try:
            value = OutcomeReview.model_validate(review["outcome_review"])
            if not value.watched or sorted(o.expectation_index for o in value.outcomes) != list(
                range(len(review.get("expectations", [])))
            ):
                raise ValueError("incomplete_review")
            if not value.listened and any(
                o.category in {"audio_source", "audio_mix", "captions"}
                and o.verdict not in {"unclear", "creator_changed"}
                for o in value.outcomes
            ):
                raise ValueError("unheard_audio")
        except (ValidationError, ValueError, TypeError):
            counts["invalid_output_reviews"] += 1
            continue
        try:
            actual = await current_export(db, case, value.artifact.variant_id, sign=False)
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            counts["unavailable_output_reviews"] += 1
            continue
        if actual.get("artifact") != value.artifact.model_dump():
            counts["stale_output_reviews"] += 1
            continue
        counts["export_reviewed_sessions"] += 1
        verdicts = Counter(o.verdict for o in value.outcomes)
        if verdicts["creator_changed"]:
            delivery["creator_changed_sessions"] += 1
        elif verdicts["met"] == len(value.outcomes):
            delivery["fulfilled_reviewed_sessions"] += 1
        elif verdicts["partial"] or verdicts["missed"]:
            delivery["unfulfilled_reviewed_sessions"] += 1
        for verdict, count in verdicts.items():
            requirements[verdict] += count
            requirements["total"] += count
        case_findings = []
        for outcome in value.outcomes:
            if outcome.verdict in {"partial", "missed"}:
                misses[outcome.category].add(str(case.id))
                findings[outcome.category] += 1
                case_findings.append({"category": outcome.category, "verdict": outcome.verdict})
        sessions.append(
            {
                "case_id": str(case.id),
                "activity_at": activity,
                "reviewed_at": review.get("reviewed_at"),
                "verdicts": dict(verdicts),
                "findings": case_findings,
            }
        )
    actions = [
        {
            "category": category,
            "instruction": CATEGORIES[category][0],
            "next_action": CATEGORIES[category][1],
            "sessions": len(ids),
            "missed_requirements": findings[category],
            "case_ids": sorted(ids),
        }
        for category, ids in misses.items()
    ]
    actions.sort(key=lambda a: (-a["sessions"], -a["missed_requirements"], a["category"]))
    delivery["unknown_fulfillment_sessions"] = (
        delivery["request_sessions"]
        - delivery["fulfilled_reviewed_sessions"]
        - delivery["unfulfilled_reviewed_sessions"]
    )
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "window_days": 14,
        "truncated": len(rows) > 1000,
        "counts": counts,
        "delivery": {
            **delivery,
            "first_attempt_fulfillment": None,
            "photos_save_acknowledgements": None,
            "scope": "eligible recent captured sessions; original-request retry lineage is unknown",
        },
        "requirements": requirements,
        "sessions": sessions,
        "next_actions": actions,
        "claim": (
            "Human-reviewed current exports from eligible recent sessions. "
            "Selected sample, not a population success rate. "
            "Unknowns remain unresolved. No live model evaluation."
        ),
    }
