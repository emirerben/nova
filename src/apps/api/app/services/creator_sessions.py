"""Durable state and safe context helpers for Main Creator Agent v1."""

from __future__ import annotations

import asyncio
import hashlib
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._schemas.creator_agent import (
    CreativeStrategy,
    CreatorCatalogRef,
    CreatorEditPlan,
    CreatorEditSnapshot,
    CreatorMediaRef,
    canonical_context_hash,
)
from app.config import settings
from app.models import (
    ContentPlan,
    CreatorAgentEvent,
    CreatorAgentExecution,
    CreatorAgentSession,
    Job,
    MusicTrack,
    Persona,
    PlanItem,
    PlanItemAsset,
    SoundEffect,
)
from app.schemas.edit_proposal import parse_edit_proposal
from app.services.creator_capabilities import resolve_creator_manifest
from app.services.edit_proposal_limits import (
    CREATOR_EXECUTION_RECEIPT_LEASE_S,
    EDIT_PROPOSAL_TASK_HARD_TIME_LIMIT_S,
    edit_proposal_task_id,
    queue_for_guided_contract,
)
from app.services.job_status import PLAN_ITEM_JOB_FAILED, PLAN_ITEM_JOB_READY

ACTIVE_CREATOR_PHASES = frozenset(
    {
        "briefing",
        "planning",
        "awaiting_confirmation",
        "executing",
        "rendering",
        "reviewing",
        "awaiting_feedback",
        "revising",
    }
)
TERMINAL_CREATOR_PHASES = frozenset({"completed", "failed", "cancelled"})
MAX_PUBLIC_EVENTS = 40
MAX_CREATOR_MEDIA_REFS = 50
CREATOR_CONTEXT_MAX_CHARS = 3900
EXECUTION_RECEIPT_LEASE_S = CREATOR_EXECUTION_RECEIPT_LEASE_S

_PHASE_TO_PUBLIC = {
    "briefing": "briefing",
    "planning": "planning",
    "awaiting_confirmation": "awaiting_confirmation",
    "executing": "executing",
    "rendering": "rendering",
    "reviewing": "reviewing",
    "awaiting_feedback": "awaiting_feedback",
    "revising": "revising",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


def rollout_eligible(user_id: uuid.UUID) -> bool:
    """Stable user allocation; rollout changes never reshuffle existing IDs."""

    if not settings.main_creator_agent_enabled:
        return False
    percent = settings.main_creator_agent_rollout_percent
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    bucket = int(hashlib.sha256(user_id.bytes).hexdigest()[:8], 16) % 100
    return bucket < percent


def _clean(value: object, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def _positive_duration_s(value: object) -> float | None:
    try:
        duration_s = float(value or 0) or None
    except (TypeError, ValueError):
        return None
    if duration_s is None or duration_s <= 0 or not math.isfinite(duration_s):
        return None
    return duration_s


def _job_matches_guided_attempt(job: Job | None, attempt_id: str | None) -> bool:
    if job is None or not attempt_id:
        return False
    assembly = job.assembly_plan or {}
    return any(
        isinstance(snapshot, dict) and snapshot.get("generation_attempt_id") == attempt_id
        for snapshot in (
            assembly.get("guided_edit"),
            assembly.get("creator_guided_fallback"),
        )
    )


def _session_variant_target(
    session: CreatorAgentSession, variants: list[object]
) -> tuple[dict[str, Any] | None, str]:
    """Resolve only the session's exact variant/generation once it is pinned."""

    if session.target_variant_id:
        if not session.target_generation_id:
            return None, "stale"
        exact = next(
            (
                variant
                for variant in variants
                if isinstance(variant, dict)
                and variant.get("variant_id") == session.target_variant_id
            ),
            None,
        )
        if exact is None:
            return None, "stale"
        generation_id = str(exact.get("render_generation_id") or "") or None
        if session.target_generation_id and generation_id != session.target_generation_id:
            return None, "stale"
        if exact.get("render_status") == "failed":
            return None, "failed"
        if exact.get("render_status") != "ready":
            return None, "processing"
        if generation_id is None:
            return None, "stale"
        return exact, "ready"

    ready = next(
        (
            variant
            for variant in variants
            if isinstance(variant, dict)
            and variant.get("render_status") == "ready"
            and variant.get("variant_id")
            and variant.get("render_generation_id")
        ),
        None,
    )
    return (ready, "ready") if ready is not None else (None, "stale")


def _partial_variant_render_in_flight(job: Job) -> bool:
    """Whether a partial-ready Job still has a variant retry in progress."""

    if job.status != "variants_ready_partial":
        return False
    variants = (job.assembly_plan or {}).get("variants") or []
    return any(
        isinstance(variant, dict) and variant.get("render_status") == "rendering"
        for variant in variants
    )


def creator_context(persona: Persona, item: PlanItem) -> tuple[str, str]:
    data = persona.persona or {}
    creator = {
        "summary": _clean(data.get("summary"), 1200),
        "tone": _clean(data.get("tone"), 300),
        "audience": _clean(data.get("audience"), 500),
        "content_pillars": [
            _clean(value, 160) for value in (data.get("content_pillars") or [])[:8]
        ],
        "style": persona.style or {},
    }
    item_data = {
        "idea": _clean(item.idea, 1200),
        "theme": _clean(item.theme, 300),
        "creator_notes": _clean(item.notes, 1200),
        "filming_guide": item.filming_guide or [],
        "current_edit_format": item.edit_format,
        "current_audio_mode": item.audio_mode,
        "has_recorded_voiceover": bool(item.voiceover_gcs_path),
    }
    # Both fields are user-influenced JSONB. Bound the fully serialized value
    # before Pydantic/model construction so an unusually rich persona or
    # filming guide degrades to partial context instead of a route-level 500.
    return (
        _clean(creator, CREATOR_CONTEXT_MAX_CHARS),
        _clean(item_data, CREATOR_CONTEXT_MAX_CHARS),
    )


async def resolve_item_creator_context(
    db: AsyncSession,
    item: PlanItem,
    *,
    persona: Persona,
) -> tuple[Any, list[dict[str, Any]]]:
    """Build an opaque manifest plus bounded footage evidence for the model."""

    media_refs: list[CreatorMediaRef] = []
    media_context: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, assignment in enumerate((item.clip_assignments or [])[:MAX_CREATOR_MEDIA_REFS]):
        if not isinstance(assignment, dict):
            continue
        media_id = _clean(assignment.get("media_id"), 160) or f"clip-{index + 1}"
        if media_id in seen:
            continue
        seen.add(media_id)
        user_note = _clean(assignment.get("user_note"), 400)
        duration_s = _positive_duration_s(assignment.get("duration_s"))
        media_refs.append(
            CreatorMediaRef(
                media_id=media_id,
                kind=(
                    assignment.get("kind")
                    if assignment.get("kind") in {"video", "image"}
                    else "video"
                ),
                duration_s=duration_s,
                label=user_note or None,
            )
        )
        media_context.append(
            {
                "media_id": media_id,
                "kind": (
                    assignment.get("kind")
                    if assignment.get("kind") in {"video", "image"}
                    else "video"
                ),
                "duration_s": duration_s,
                "creator_note": user_note or None,
            }
        )

    assets = (
        await db.execute(
            select(PlanItemAsset)
            .where(
                PlanItemAsset.plan_item_id == item.id,
                PlanItemAsset.user_id == persona.user_id,
                PlanItemAsset.status == "ready",
                PlanItemAsset.deduplicated_to_asset_id.is_(None),
            )
            .order_by(PlanItemAsset.created_at)
            .limit(50)
        )
    ).scalars()
    for asset in assets:
        if len(media_refs) >= MAX_CREATOR_MEDIA_REFS:
            break
        media_id = f"asset-{asset.id}"
        if media_id in seen:
            continue
        seen.add(media_id)
        kind = asset.kind if asset.kind in {"video", "image"} else "image"
        context = _clean(asset.user_context, 400)
        media_refs.append(
            CreatorMediaRef(
                media_id=media_id,
                kind=kind,
                duration_s=_positive_duration_s(asset.duration_s),
                label=context or None,
            )
        )
        analysis = asset.analysis if isinstance(asset.analysis, dict) else {}
        media_context.append(
            {
                "media_id": media_id,
                "kind": kind,
                "duration_s": _positive_duration_s(asset.duration_s),
                "creator_context": context or None,
                # AI evidence is clearly segregated and must never be copied to
                # on-screen text (also enforced in the main prompt).
                "analysis_only_not_copy": {
                    key: _clean(analysis.get(key), 400)
                    for key in ("summary", "description", "setting", "activity")
                    if analysis.get(key)
                },
            }
        )

    # clip_gcs_paths is the legacy source of truth for older rows without
    # clip_assignments. Expose stable opaque IDs, never the paths themselves.
    if not media_refs:
        for index, _path in enumerate((item.clip_gcs_paths or [])[:MAX_CREATOR_MEDIA_REFS]):
            media_id = f"legacy-clip-{index + 1}"
            media_refs.append(CreatorMediaRef(media_id=media_id, kind="video"))
            media_context.append({"media_id": media_id, "kind": "video"})

    tracks = (
        await db.execute(
            select(MusicTrack)
            .where(
                MusicTrack.analysis_status == "ready",
                MusicTrack.published_at.is_not(None),
                MusicTrack.archived_at.is_(None),
            )
            .order_by(MusicTrack.published_at.desc())
            .limit(30)
        )
    ).scalars()
    catalog = [
        CreatorCatalogRef(
            catalog_id=f"music-{track.id}",
            kind="music",
            label=_clean(" — ".join(value for value in (track.title, track.artist) if value), 160),
        )
        for track in tracks
    ]
    # The planner may propose a licensed SFX command only from this bounded,
    # server-owned catalog.  Expose the effect's opaque primary key (the
    # craft route resolves and revalidates it); never expose its storage path.
    sound_effects = (
        await db.execute(
            select(SoundEffect)
            .where(
                SoundEffect.status == "ready",
                SoundEffect.published_at.is_not(None),
                SoundEffect.archived_at.is_(None),
            )
            .order_by(SoundEffect.published_at.desc(), SoundEffect.created_at.desc())
            .limit(max(0, 50 - len(catalog)))
        )
    ).scalars()
    catalog.extend(
        CreatorCatalogRef(
            catalog_id=str(effect.id),
            kind="sound_effect",
            label=_clean(effect.name, 160),
        )
        for effect in sound_effects
    )
    has_ready_variant = False
    current_edit: CreatorEditSnapshot | None = None
    if item.current_job_id:
        job = await db.get(Job, item.current_job_id)
        has_ready_variant = bool(job and job.status in PLAN_ITEM_JOB_READY)
        if job is not None:
            variants = (job.assembly_plan or {}).get("variants") or []
            primary = next(
                (
                    value
                    for value in variants
                    if isinstance(value, dict) and value.get("variant_id")
                ),
                None,
            )
            if job.status in PLAN_ITEM_JOB_READY:
                edit_status = "ready"
            elif job.status in PLAN_ITEM_JOB_FAILED:
                edit_status = "failed"
            else:
                edit_status = "generating"
            edit_identity = {
                "job_id": str(job.id),
                "status": job.status,
                "variants": [
                    {
                        "variant_id": value.get("variant_id"),
                        "render_generation_id": value.get("render_generation_id"),
                        "render_status": value.get("render_status"),
                    }
                    for value in variants
                    if isinstance(value, dict)
                ],
            }
            current_edit = CreatorEditSnapshot(
                status=edit_status,
                variant_id=str(primary["variant_id"]) if primary else None,
                edit_hash=canonical_context_hash(edit_identity),
            )
    manifest = resolve_creator_manifest(
        item_id=str(item.id),
        edit_format=item.edit_format,
        has_voiceover=(item.audio_mode == "voiceover" and bool(item.voiceover_gcs_path)),
        media=media_refs,
        catalog=catalog,
        current_edit=current_edit,
        has_ready_variant=has_ready_variant,
    )
    return manifest, media_context


async def append_event(
    db: AsyncSession,
    session: CreatorAgentSession,
    *,
    event_type: str,
    payload: dict[str, Any],
    client_event_id: str | None = None,
    role: str | None = None,
) -> CreatorAgentEvent:
    sequence = (
        int(
            (
                await db.execute(
                    select(func.coalesce(func.max(CreatorAgentEvent.sequence), -1)).where(
                        CreatorAgentEvent.session_id == session.id
                    )
                )
            ).scalar_one()
        )
        + 1
    )
    session.revision += 1
    event = CreatorAgentEvent(
        session_id=session.id,
        sequence=sequence,
        client_event_id=client_event_id,
        role=role
        or (
            "user"
            if event_type.startswith("user_")
            else "assistant"
            if event_type.startswith("assistant_")
            else "system"
        ),
        event_type=event_type,
        payload=payload,
        revision=session.revision,
    )
    db.add(event)
    return event


def compile_active_plan(
    session: CreatorAgentSession,
    *,
    manifest: Any,
    strategy: CreativeStrategy,
    summary: str,
    creator_request: str = "",
) -> dict[str, Any]:
    """Compile an inert, hash-pinned plan with a deterministic public receipt."""

    # Imported lazily so schema-only tests can import this service while the
    # compiler evolves independently.
    from app.services.creator_capabilities import compile_strategy_to_plan  # noqa: PLC0415

    edit_plan: CreatorEditPlan = compile_strategy_to_plan(manifest, strategy)
    prior_version = int((session.active_plan or {}).get("version", 0))
    receipt = {
        "version": prior_version + 1,
        "summary": _clean(summary, 1000) or _clean(strategy.rationale, 1000),
        "creative_rationale": _clean(strategy.rationale, 2000),
        "edit_format": getattr(strategy, "edit_format", None) or manifest.edit_format,
        "audio_strategy": getattr(strategy, "audio_strategy", None),
        "story_structure": list(getattr(strategy, "story_structure", []) or []),
        "caption_style": getattr(strategy, "caption_style", None),
        "intro_hook": getattr(strategy, "intro_hook", None),
        "target_duration_s": strategy.target_duration_s,
        "edit_plan": edit_plan.model_dump(mode="json", exclude_none=True),
    }
    clean_request = _clean(creator_request, 1000)
    if clean_request:
        receipt["creator_request"] = clean_request
    if strategy.mixed_media_timing is not None:
        receipt["mixed_media_timing"] = strategy.mixed_media_timing.model_dump(mode="json")
    if strategy.montage_cadence is not None:
        receipt["montage_cadence"] = strategy.montage_cadence.model_dump(mode="json")
    receipt["plan_hash"] = canonical_context_hash(receipt)
    return receipt


async def reconcile_render_state(db: AsyncSession, session: CreatorAgentSession) -> bool:
    """Advance a rendering session against its exact target Job generation."""

    raw_pending_review = getattr(session, "last_review", None)
    pending_review = raw_pending_review if isinstance(raw_pending_review, dict) else {}
    if session.phase not in {"executing", "rendering", "reviewing", "awaiting_feedback"}:
        return False
    if session.phase == "awaiting_feedback" and pending_review.get("status") != "pending":
        # A broker outage is the one transient review state that reconciliation
        # may retry.  Disabled/failed/stale reviews remain terminal manual
        # feedback receipts and must not be republished indefinitely.
        retryable_review_enqueue = pending_review.get("status") == "unavailable" and (
            pending_review.get("dispatch_status") == "failed"
            or pending_review.get("error_code") == "review_enqueue_failed"
        )
        if not retryable_review_enqueue:
            return False
    if not session.target_job_id:
        item = await db.get(
            PlanItem,
            session.plan_item_id,
            with_for_update=True,
            populate_existing=True,
        )
        if item is None:
            return False
        receipt = (
            await db.execute(
                select(CreatorAgentExecution)
                .where(
                    CreatorAgentExecution.session_id == session.id,
                    CreatorAgentExecution.status.in_(("running", "succeeded")),
                )
                .order_by(CreatorAgentExecution.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        active = session.active_plan or {}
        raw_plan = active.get("edit_plan") if isinstance(active, dict) else None
        expected_strategy = raw_plan.get("strategy") if isinstance(raw_plan, dict) else None
        expected_program = (
            expected_strategy.get("render_program") if isinstance(expected_strategy, dict) else None
        )
        expected_guided_attempt = active.get("guided_generation_attempt_id")
        candidate = await db.get(Job, item.current_job_id) if item.current_job_id else None
        exact_plan = bool(
            candidate
            and (
                (
                    expected_program == "guided"
                    and _job_matches_guided_attempt(candidate, expected_guided_attempt)
                )
                or (
                    expected_program == "native"
                    and (candidate.all_candidates or {}).get("creator_strategy")
                    == expected_strategy
                )
            )
        )
        exact_job = bool(
            receipt is not None
            and candidate is not None
            and candidate.created_at is not None
            and receipt.created_at is not None
            and candidate.created_at >= receipt.created_at
            and candidate.user_id == session.creator_id
            and candidate.content_plan_item_id == session.plan_item_id
            and candidate.content_plan_ownership_epoch == session.ownership_epoch
            and exact_plan
        )
        raw_proposal_state = getattr(item, "edit_proposal", None)
        proposal_state = raw_proposal_state if isinstance(raw_proposal_state, dict) else {}
        failure = proposal_state.get("failure")
        proposal_code = failure.get("code") if isinstance(failure, dict) else None
        from app.schemas.edit_proposal import MAIN_CREATOR_FAIL_CLOSED  # noqa: PLC0415

        exact_failed_creator_attempt = bool(
            receipt is not None
            and expected_guided_attempt
            and proposal_state.get("generation_attempt_id") == expected_guided_attempt
            and proposal_state.get("status") == "failed"
            and proposal_state.get("design_fallback") == MAIN_CREATOR_FAIL_CLOSED
        )
        if exact_failed_creator_attempt:
            # The planner has already committed the terminal proposal failure.
            # Reconcile the matching controller + receipt in this transaction
            # on the next poll, without waiting for the 10-minute dispatch
            # lease. The stable attempt identity prevents a stale failure from
            # terminating a newer Creator execution.
            failure_code = proposal_code or "proposal_generation_failed"
            receipt.status = "failed"
            receipt.error = {"code": failure_code}
            receipt.completed_at = datetime.now(UTC)
            session.phase = "failed"
            session.last_error = {
                "code": failure_code,
                "message": "Kria couldn't plan this direction. Try it again.",
            }
            await append_event(
                db,
                session,
                event_type="assistant_render_failed",
                payload={"message": "I couldn't plan that direction. Try it again."},
            )
            return True
        if not exact_job:
            receipt_lease_expired = bool(
                receipt is not None
                and receipt.created_at is not None
                and datetime.now(UTC) - receipt.created_at
                >= timedelta(seconds=EXECUTION_RECEIPT_LEASE_S)
            )
            parsed_proposal = parse_edit_proposal(raw_proposal_state)
            exact_active_proposal = bool(
                parsed_proposal is not None
                and parsed_proposal.generation_attempt_id == expected_guided_attempt
                and parsed_proposal.status in {"analyzing", "drafting"}
            )
            proposal_task_still_valid = bool(
                exact_active_proposal
                and parsed_proposal is not None
                and parsed_proposal.planning_started_at is not None
                and datetime.now(UTC) - parsed_proposal.planning_started_at
                < timedelta(seconds=EDIT_PROPOSAL_TASK_HARD_TIME_LIMIT_S)
            )
            if (
                receipt_lease_expired
                and exact_active_proposal
                and parsed_proposal is not None
                and parsed_proposal.planning_started_at is None
            ):
                from app.services.queue_state import get_task_runtime_state  # noqa: PLC0415
                from app.worker import celery_app  # noqa: PLC0415

                queue_name = queue_for_guided_contract(
                    parsed_proposal.brief.mixed_media_timing,
                    parsed_proposal.brief.montage_cadence,
                    default_queue=settings.pool_asset_analysis_queue,
                )
                runtime = await asyncio.to_thread(
                    get_task_runtime_state,
                    celery_app,
                    edit_proposal_task_id(parsed_proposal.generation_attempt_id),
                    queue_name=queue_name,
                )
                proposal_task_still_valid = runtime.state != "not_found"
            lease_expired = receipt_lease_expired and not proposal_task_still_valid
            if (
                not lease_expired
                or receipt is None
                or not (receipt.status == "running" or expected_guided_attempt)
            ):
                return False

            if expected_guided_attempt:
                from app.services.edit_proposals import (  # noqa: PLC0415
                    expire_proposal_attempt,
                )

                expire_proposal_attempt(
                    item,
                    generation_attempt_id=str(expected_guided_attempt),
                )
            failure_code = proposal_code or "execution_lease_expired"
            receipt.status = "failed"
            receipt.error = {"code": failure_code}
            receipt.completed_at = datetime.now(UTC)
            session.phase = "failed"
            session.last_error = {
                "code": failure_code,
                "message": "The render did not start. Try this direction again.",
            }
            await append_event(
                db,
                session,
                event_type="assistant_render_failed",
                payload={"message": "That render didn't start. Try this direction again."},
            )
            return True
        if candidate is None:  # narrowed by exact_job
            return False
        session.target_job_id = candidate.id
    job = await db.get(Job, session.target_job_id)
    if job is None:
        session.phase = "failed"
        session.last_error = {"code": "target_job_missing", "message": "The render disappeared."}
        await append_event(
            db,
            session,
            event_type="system_render_failed",
            payload={"message": "The render couldn't be found. Start a new version."},
        )
        return True
    item = await db.get(PlanItem, session.plan_item_id)
    plan = await db.get(ContentPlan, item.content_plan_id) if item is not None else None
    exact_owner = bool(
        item is not None
        and plan is not None
        and plan.user_id == session.creator_id
        and int(plan.ownership_epoch or 0) == session.ownership_epoch
        and job.user_id == session.creator_id
        and job.content_plan_item_id == session.plan_item_id
        and job.content_plan_ownership_epoch == session.ownership_epoch
    )
    if not exact_owner:
        session.phase = "failed"
        session.last_error = {"code": "render_identity_mismatch"}
        await append_event(
            db,
            session,
            event_type="system_render_failed",
            payload={"message": "The render identity changed. Start a new creator session."},
        )
        return True
    # ``variants_ready_partial`` is terminal for the initial orchestrator, but
    # not while a failed variant is being retried in place.  Keep the Creator
    # session in rendering until that exact variant's generation-fenced worker
    # publishes ready/failed; otherwise a GET poll can incorrectly settle the
    # session and permit a second mutation while work is still running.
    if _partial_variant_render_in_flight(job):
        changed = session.phase != "rendering"
        session.phase = "rendering"
        return changed
    if job.status in PLAN_ITEM_JOB_FAILED:
        session.phase = "failed"
        session.last_error = {"code": job.failure_reason or "render_failed"}
        await append_event(
            db,
            session,
            event_type="assistant_render_failed",
            payload={"message": "That render didn't finish. Your confirmed plan is saved."},
        )
        return True
    if job.status in PLAN_ITEM_JOB_READY:
        variants = (job.assembly_plan or {}).get("variants") or []
        ready_variant, target_state = _session_variant_target(session, variants)
        if target_state == "processing":
            changed = session.phase != "rendering"
            session.phase = "rendering"
            return changed
        if ready_variant is None:
            session.phase = "failed"
            session.last_error = {
                "code": "render_target_failed"
                if target_state == "failed"
                else "render_identity_mismatch"
            }
            await append_event(
                db,
                session,
                event_type="system_render_failed",
                payload={"message": "The selected render changed. Start a new version."},
            )
            return True
        was_awaiting_feedback = session.phase == "awaiting_feedback"
        session.phase = "awaiting_feedback"
        session.target_variant_id = str(ready_variant["variant_id"])
        session.target_generation_id = str(ready_variant["render_generation_id"])
        session.last_good = {
            "job_id": str(job.id),
            "plan_hash": (session.active_plan or {}).get("plan_hash"),
        }
        review_enabled = settings.main_creator_agent_review_enabled
        quality_review_enabled = (
            review_enabled and settings.main_creator_agent_quality_review_enabled
        )
        if quality_review_enabled and session.target_variant_id and session.target_generation_id:
            from app.tasks.creator_quality_review import (  # noqa: PLC0415
                queue_creator_quality_review,
            )

            queue_creator_quality_review(
                session,
                job_id=str(job.id),
                variant_id=str(session.target_variant_id),
                render_generation_id=str(session.target_generation_id),
            )
        elif quality_review_enabled:
            session.last_review = {
                "status": "unavailable",
                "decision": "unavailable",
                "error_code": "review_target_missing",
                "error_message": "The ready render has no exact variant generation to review.",
                "job_id": str(job.id),
                "failed_at": datetime.now(UTC).isoformat(),
            }
        elif pending_review.get("status") in {"pending", "running"}:
            # The kill switch may flip after a pending receipt commits but
            # before the worker can close it. Poll-time reconciliation owns the
            # same exact session/Job/variant/generation rows, so make the
            # manual-feedback fallback terminal here instead of polling
            # forever. A stale worker is still fenced by the review key.
            session.last_review = {
                **pending_review,
                "status": "unavailable",
                "decision": "unavailable",
                "error_code": "review_disabled",
                "error_message": (
                    "Quality review is disabled; watch the render and give manual feedback."
                ),
                "failed_at": datetime.now(UTC).isoformat(),
            }
        elif review_enabled:
            session.last_review = {
                "decision": "approve",
                "mode": "structural_v1",
                "job_id": str(job.id),
                "variant_id": session.target_variant_id,
                "generation_id": session.target_generation_id,
                "reviewed_at": datetime.now(UTC).isoformat(),
            }
        if was_awaiting_feedback:
            return True
        await append_event(
            db,
            session,
            event_type="assistant_review" if review_enabled else "assistant_execution",
            payload={
                "message": (
                    "The confirmed edit is ready. Watch it and tell me what feels "
                    "right or what you want changed."
                ),
                **({"decision": "approve"} if review_enabled else {}),
            },
        )
        return True
    if session.phase == "executing":
        session.phase = "rendering"
        return True
    return False


def serialize_session(session: CreatorAgentSession) -> dict[str, Any]:
    events = sorted(session.events or [], key=lambda event: event.sequence)[-MAX_PUBLIC_EVENTS:]

    def role(event_type: str) -> str:
        if event_type.startswith("user_"):
            return "user"
        if event_type.startswith("assistant_"):
            return "assistant"
        return "system"

    raw_review = getattr(session, "last_review", None)
    review: dict[str, Any] | None = None
    if isinstance(raw_review, dict):
        # Keep the public receipt bounded and omit any future provider/debug
        # fields that might be added to the durable JSONB payload.
        allowed = {
            "status",
            "review_key",
            "creator_id",
            "creator_session_id",
            "plan_item_id",
            "ownership_epoch",
            "session_revision",
            "job_id",
            "variant_id",
            "render_generation_id",
            "generation_id",
            "manifest_hash",
            "context_hash",
            "review_mode",
            "mode",
            "decision",
            "reviewer",
            "quality_score",
            "confidence",
            "reviewed_at",
            "queued_at",
            "started_at",
            "failed_at",
            "error_code",
            "error_message",
            "evidence",
            "proposed_revision",
            "objective_tag",
            "expected_improvement",
            "allowlist_action",
            "automatic_revision_count",
            "rollback_receipt",
            "auto_iteration",
        }
        review = {key: raw_review[key] for key in allowed if key in raw_review}
        evidence = review.get("evidence")
        if isinstance(evidence, list):
            review["evidence"] = [
                {
                    key: value
                    for key in (
                        "evidence_id",
                        "kind",
                        "severity",
                        "start_s",
                        "end_s",
                        "observation",
                    )
                    if key in value
                }
                for value in evidence[:12]
                if isinstance(value, dict)
            ]
        proposal = review.get("proposed_revision")
        if isinstance(proposal, dict):
            review["proposed_revision"] = {
                key: proposal[key]
                for key in ("revision_id", "summary", "rationale", "evidence_ids")
                if key in proposal
            }
            if isinstance(review["proposed_revision"].get("evidence_ids"), list):
                review["proposed_revision"]["evidence_ids"] = review["proposed_revision"][
                    "evidence_ids"
                ][:8]
        auto_iteration = review.get("auto_iteration")
        if isinstance(auto_iteration, dict):
            bounded_auto = {
                key: auto_iteration[key]
                for key in (
                    "status",
                    "action",
                    "session_id",
                    "job_id",
                    "variant_id",
                    "previous_generation_id",
                    "generation_id",
                    "receipt_id",
                    "request_expected_revision",
                    "expected_revision",
                    "ownership_epoch",
                )
                if key in auto_iteration
            }
            rollback = auto_iteration.get("rollback_receipt")
            if isinstance(rollback, dict):
                bounded_auto["rollback_receipt"] = {
                    key: rollback[key]
                    for key in (
                        "job_id",
                        "variant_id",
                        "previous_generation_id",
                        "craft_receipt_id",
                    )
                    if key in rollback
                }
            review["auto_iteration"] = bounded_auto

    return {
        "id": str(session.id),
        "status": _PHASE_TO_PUBLIC.get(session.phase, "failed"),
        "revision": session.revision,
        "render_attempts": session.render_attempts,
        "max_render_attempts": session.max_render_attempts,
        "can_render": settings.main_creator_agent_execution_enabled,
        "pending_plan": session.active_plan if session.phase == "awaiting_confirmation" else None,
        "current_job_id": str(session.target_job_id) if session.target_job_id else None,
        "last_review": review,
        "auto_iteration": (
            {
                "available": bool(
                    settings.main_creator_agent_execution_enabled
                    and settings.main_creator_agent_review_enabled
                    and settings.main_creator_agent_quality_review_enabled
                    and settings.main_creator_agent_auto_iteration_enabled
                ),
                "label": "One objective revision, if eligible",
            }
            if settings.main_creator_agent_auto_iteration_enabled
            else None
        ),
        "events": [
            {
                "id": str(event.id),
                "role": event.role or role(event.event_type),
                "event_type": event.event_type,
                "payload": event.payload or {},
                "created_at": event.created_at.isoformat() if event.created_at else "",
            }
            for event in events
        ],
        "created_at": session.created_at.isoformat() if session.created_at else "",
        "updated_at": session.updated_at.isoformat() if session.updated_at else "",
    }


__all__ = [
    "ACTIVE_CREATOR_PHASES",
    "TERMINAL_CREATOR_PHASES",
    "append_event",
    "compile_active_plan",
    "creator_context",
    "reconcile_render_state",
    "resolve_item_creator_context",
    "rollout_eligible",
    "serialize_session",
]
