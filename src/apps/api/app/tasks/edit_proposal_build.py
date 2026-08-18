"""Background media analysis and proposal drafting for Plan edit."""

from __future__ import annotations

import math
import os
import tempfile
import uuid
from pathlib import Path

import structlog
from billiard.exceptions import SoftTimeLimitExceeded
from celery.exceptions import MaxRetriesExceededError, Retry
from sqlalchemy import select

from app.database import sync_session
from app.models import ContentPlan, PlanItem, PlanItemAsset
from app.schemas.edit_proposal import (
    EditProposal,
    EditProposalSnapshot,
    MediaRef,
    ProposalFailure,
    StoryBeat,
    canonical_media_digest,
    parse_edit_proposal,
)
from app.services.content_plan_persona import load_owned_plan_persona_sync
from app.services.edit_proposals import media_generations_match_sync, save_proposal_draft
from app.worker import celery_app

log = structlog.get_logger()

_TASK_LIMITS = {"soft_time_limit": 540, "time_limit": 600}


def _locked_item(
    db,
    item_id: uuid.UUID,
    expected_ownership_epoch: int,  # noqa: ANN001
) -> tuple[PlanItem, uuid.UUID] | None:
    ref = db.get(PlanItem, item_id)
    if ref is None:
        return None
    plan = db.get(ContentPlan, ref.content_plan_id, with_for_update=True)
    if plan is None:
        return None
    load_owned_plan_persona_sync(db, plan, for_update=True)
    if int(getattr(plan, "ownership_epoch", 0) or 0) != expected_ownership_epoch:
        return None
    item = db.get(PlanItem, item_id, populate_existing=True, with_for_update=True)
    if item is None or item.content_plan_id != plan.id:
        return None
    return item, plan.user_id


def _kind(content_type: str, path: str) -> str:
    if content_type.startswith("image/") or Path(path).suffix.lower() in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".heic",
    }:
        return "image"
    return "video"


# No artificial duration floor: the planner adapts to whatever footage is
# actually uploaded (product decision, 2026-08-18 — see agents/DECISIONS.md).
# Below this, a guided story cannot show anything meaningful.
MIN_GUIDED_DURATION_S = 3
# Images are not length-constrained by a source clip, so estimating "feasible"
# story length can't sum their real duration the way video works. Credit each
# image the guided_story per-media floor (guided_story.py STYLE_POLICY
# "guided_story"."min_moment_s") rather than an unbounded amount.
_IMAGE_FEASIBLE_CREDIT_S = 1.4


def feasible_guided_duration_s(media: list[MediaRef]) -> float:
    """Conservative estimate of story length the uploaded media can support.

    Videos contribute their own probed duration once; a beat can never be
    stretched past what was actually filmed (no slow-mo/loop). This is a
    pre-agent planning estimate — guided_story.py's `_source_window` /
    `_allocate_beat_durations` remain the exact, authoritative render-time
    feasibility check.
    """

    total = 0.0
    for ref in media:
        if ref.kind == "video" and ref.duration_s:
            total += float(ref.duration_s)
        else:
            total += _IMAGE_FEASIBLE_CREDIT_S
    return total


def adapt_target_duration_s(brief_duration_s: int, feasible_s: float) -> int:
    """Clamp the brief's target to what the footage can actually support.

    Never exceeds the feasible estimate (floored, so the agent's target is
    never longer than real footage allows) and never exceeds the creator's
    requested duration. Callers must treat
    ``feasible_s < MIN_GUIDED_DURATION_S`` as infeasible before calling this.
    """

    return max(MIN_GUIDED_DURATION_S, min(int(brief_duration_s), math.floor(feasible_s)))


def _fail(
    item: PlanItem,
    proposal: EditProposal,
    code: str,
    message: str,
    *,
    retryable: bool = True,
    detail: str | None = None,
) -> None:
    failed = proposal.model_copy(
        update={
            "proposal_version": proposal.proposal_version + 1,
            "status": "failed",
            "failure": ProposalFailure(
                code=code, message=message, retryable=retryable, detail=detail
            ),
        }
    )
    item.edit_proposal = failed.model_dump(mode="json")


def _exc_detail(exc: BaseException) -> str:
    """Admin/debug-only diagnostic string — never surfaced to end users.

    _edit_proposal_response() in routes/plan_items.py strips ProposalFailure.detail
    before the public PlanItem response is built.
    """

    return f"{type(exc).__name__}: {exc}"[:2000]


def _pool_refs(db, item: PlanItem, owner_id: uuid.UUID) -> list[MediaRef]:  # noqa: ANN001
    from app import storage  # noqa: PLC0415

    all_rows = list(
        db.execute(
            select(PlanItemAsset)
            .where(PlanItemAsset.plan_item_id == item.id)
            .order_by(PlanItemAsset.created_at)
        ).scalars()
    )
    if any(row.user_id != owner_id for row in all_rows):
        raise PermissionError("plan-item asset owner mismatch")
    rows = [row for row in all_rows if row.status == "ready"]
    for row in rows:
        if not row.gcs_generation:
            row.gcs_generation = storage.object_metadata(row.gcs_path).generation
    if rows:
        db.flush()
    return [
        MediaRef(
            lane="asset",
            media_id=str(row.id),
            gcs_path=row.gcs_path,
            generation=str(row.gcs_generation),
            kind="image" if row.kind == "image" else "video",
            source_filename=row.source_filename or "",
            duration_s=float(row.duration_s) if row.duration_s else None,
            aspect=float(row.aspect) if row.aspect else None,
            content_hash=row.content_hash,
            user_context=row.user_context or "",
            analysis=dict(row.analysis or {}),
        )
        for row in rows
    ]


def _analyze_clip_assignment(raw: dict, pool_by_path: dict[str, MediaRef]) -> tuple[dict, MediaRef]:
    from app import storage  # noqa: PLC0415
    from app.tasks.autoplace import analyze_pool_image, analyze_pool_video  # noqa: PLC0415

    entry = dict(raw)
    path = str(entry["gcs_path"])
    media_id = str(entry["media_id"])
    if path in pool_by_path:
        pooled = pool_by_path[path]
        ref = pooled.model_copy(update={"lane": "clip", "media_id": media_id})
        entry.update(
            {
                "generation": ref.generation,
                "kind": ref.kind,
                "duration_s": ref.duration_s,
                "aspect": ref.aspect,
                "analysis": ref.analysis,
            }
        )
        return entry, ref

    metadata = storage.object_metadata(path)
    kind = _kind(metadata.content_type, path)
    cached = entry.get("analysis") if entry.get("generation") == metadata.generation else None
    analysis = dict(cached or {})
    duration = entry.get("duration_s")
    aspect = entry.get("aspect")
    if not analysis:
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, Path(path).name or "media")
            storage.download_generation_to_file(path, local, generation=metadata.generation)
            if kind == "image":
                result, aspect, dims, has_alpha = analyze_pool_image(local, media_id)
                analysis = result or {}
                if dims:
                    analysis.update({"width": dims[0], "height": dims[1]})
                analysis["has_alpha"] = has_alpha
            else:
                result, aspect, duration, dims = analyze_pool_video(local)
                analysis = result or {}
                if dims:
                    analysis.update({"width": dims[0], "height": dims[1]})
    entry.update(
        {
            "generation": metadata.generation,
            "kind": kind,
            "duration_s": duration,
            "aspect": aspect,
            "analysis": analysis,
        }
    )
    raw_name = Path(path).name
    ref = MediaRef(
        lane="clip",
        media_id=media_id,
        gcs_path=path,
        generation=metadata.generation,
        kind=kind,
        source_filename=raw_name.split("-", 1)[-1],
        duration_s=float(duration) if duration else None,
        aspect=float(aspect) if aspect else None,
        user_context=str(entry.get("user_note") or ""),
        analysis=analysis,
    )
    return entry, ref


def _attempt_is_active(
    item_id: uuid.UUID,
    attempt_id: str,
    expected_ownership_epoch: int,
) -> bool:
    """Cheap cancellation fence between expensive per-media analyses."""

    with sync_session() as db:
        row = db.execute(
            select(PlanItem.edit_proposal, ContentPlan.ownership_epoch)
            .join(ContentPlan, ContentPlan.id == PlanItem.content_plan_id)
            .where(PlanItem.id == item_id)
        ).one_or_none()
        current = parse_edit_proposal(row.edit_proposal) if row else None
        return bool(
            row
            and int(row.ownership_epoch or 0) == expected_ownership_epoch
            and current
            and current.generation_attempt_id == attempt_id
            and current.status == "analyzing"
        )


_ANALYSIS_FIELDS = ("generation", "kind", "duration_s", "aspect", "analysis")


def _merge_analyzed_assignments(current: list[dict], analyzed: list[dict]) -> list[dict] | None:
    """Merge analysis into live assignments without overwriting user edits."""

    analyzed_by_identity = {
        (str(row.get("media_id") or ""), str(row.get("gcs_path") or "")): row
        for row in analyzed
        if row.get("media_id") and row.get("gcs_path")
    }
    current_identities = {
        (str(row.get("media_id") or ""), str(row.get("gcs_path") or ""))
        for row in current
        if row.get("media_id") and row.get("gcs_path")
    }
    if current_identities != set(analyzed_by_identity):
        return None
    merged: list[dict] = []
    for raw in current:
        entry = dict(raw)
        source = analyzed_by_identity[(str(entry["media_id"]), str(entry["gcs_path"]))]
        for field in _ANALYSIS_FIELDS:
            if field in source:
                entry[field] = source[field]
        merged.append(entry)
    return merged


@celery_app.task(
    bind=True,
    name="app.tasks.edit_proposal_build.draft_edit_proposal",
    max_retries=40,
    default_retry_delay=15,
    **_TASK_LIMITS,
)
def draft_edit_proposal(self, item_id: str, attempt_id: str, expected_ownership_epoch: int) -> None:  # noqa: ANN001
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents.edit_proposal import (  # noqa: PLC0415
        EditProposalAgent,
        EditProposalAgentInput,
        EditProposalMedia,
    )
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    try:
        iid = uuid.UUID(item_id)
        ownership_epoch = int(expected_ownership_epoch)
    except (TypeError, ValueError):
        return

    with pipeline_trace_for(item_id):
        try:
            with sync_session() as db:
                locked = _locked_item(db, iid, ownership_epoch)
                if locked is None:
                    return
                item, owner_id = locked
                proposal = parse_edit_proposal(item.edit_proposal)
                if proposal is None or proposal.generation_attempt_id != attempt_id:
                    return
                pool_rows = db.execute(
                    select(PlanItemAsset.status, PlanItemAsset.user_id).where(
                        PlanItemAsset.plan_item_id == item.id
                    )
                ).all()
                if any(row.user_id != owner_id for row in pool_rows):
                    _fail(
                        item,
                        proposal,
                        "media_ownership_mismatch",
                        "Kria couldn't safely use one of these visuals. Remove it and try again.",
                    )
                    db.commit()
                    return
                # Reservations and cleanup claims are not creator-visible media.
                # Registered assets may still be finishing their own queued
                # analysis, so keep this proposal attempt alive and retry
                # instead of turning a normal queue race into a user failure.
                registered_states = {"uploaded", "queued", "analyzing", "ready", "failed"}
                pool_states = [row.status for row in pool_rows if row.status in registered_states]
                if any(state == "failed" for state in pool_states):
                    _fail(
                        item,
                        proposal,
                        "media_analysis_failed",
                        "One or more visuals could not be analyzed. Retry or remove them.",
                    )
                    db.commit()
                    return
                if any(state != "ready" for state in pool_states):
                    try:
                        raise self.retry(countdown=15)
                    except MaxRetriesExceededError:
                        _fail(
                            item,
                            proposal,
                            "media_analysis_incomplete",
                            "Some visuals are still being analyzed. Retry or remove them.",
                        )
                        db.commit()
                        return
                pool = _pool_refs(db, item, owner_id)
                assignments = [
                    dict(a)
                    for a in (item.clip_assignments or [])
                    if isinstance(a, dict) and a.get("gcs_path") and a.get("media_id")
                ]
                idea, theme, brief = item.idea, item.theme or "", proposal.brief

            # Pool/clip media analysis (_analyze_clip_assignment -> analyze_pool_video /
            # analyze_pool_image) runs a raw Gemini call outside the Agent framework, so
            # it never produces an agent_run row — any failure here is otherwise
            # invisible to admin/debug. autoplace already distinguishes a permanently
            # unreadable file from a transient provider hiccup (same split
            # analyze_pool_asset uses); mirror that here instead of letting both
            # collapse into the outer blanket-exception handler below, which used to
            # wedge a creator behind a retryable=True failure that never actually
            # retried anything (2026-08 guided-auto-design incident).
            from app.tasks.autoplace import (  # noqa: PLC0415
                AnalysisTemporarilyUnavailableError,
                AssetUnreadableError,
            )

            pool_by_path = {row.gcs_path: row for row in pool}
            analyzed_assignments: list[dict] = []
            clip_refs: list[MediaRef] = []
            for assignment in assignments:
                if not _attempt_is_active(iid, attempt_id, ownership_epoch):
                    return
                try:
                    analyzed, ref = _analyze_clip_assignment(assignment, pool_by_path)
                except AssetUnreadableError as exc:
                    with sync_session() as db:
                        locked = _locked_item(db, iid, ownership_epoch)
                        item = locked[0] if locked else None
                        current = parse_edit_proposal(item.edit_proposal) if item else None
                        if (
                            item
                            and current
                            and current.generation_attempt_id == attempt_id
                            and current.status == "analyzing"
                        ):
                            _fail(
                                item,
                                current,
                                "media_unreadable",
                                "Kria couldn't read one of these clips. Export it as JPG, "
                                "PNG, WebP, HEIC, HEIF, MP4, or MOV and try again.",
                                retryable=False,
                                detail=_exc_detail(exc),
                            )
                            db.commit()
                    return
                except AnalysisTemporarilyUnavailableError as exc:
                    try:
                        raise self.retry(countdown=15)
                    except MaxRetriesExceededError:
                        with sync_session() as db:
                            locked = _locked_item(db, iid, ownership_epoch)
                            item = locked[0] if locked else None
                            current = parse_edit_proposal(item.edit_proposal) if item else None
                            if (
                                item
                                and current
                                and current.generation_attempt_id == attempt_id
                                and current.status == "analyzing"
                            ):
                                _fail(
                                    item,
                                    current,
                                    "media_analysis_temporarily_unavailable",
                                    "Kria temporarily couldn't analyze one of these clips. "
                                    "Try again in a bit.",
                                    detail=_exc_detail(exc),
                                )
                                db.commit()
                        return
                analyzed_assignments.append(analyzed)
                clip_refs.append(ref)
            # De-duplicate pool assets promoted into the clip lane: they remain
            # stored separately, but one object must not count twice in the story.
            clip_paths = {ref.gcs_path for ref in clip_refs}
            media = clip_refs + [ref for ref in pool if ref.gcs_path not in clip_paths]
            if not media:
                with sync_session() as db:
                    locked = _locked_item(db, iid, ownership_epoch)
                    item = locked[0] if locked else None
                    current = parse_edit_proposal(item.edit_proposal) if item else None
                    if item and current and current.generation_attempt_id == attempt_id:
                        _fail(
                            item,
                            current,
                            "proposal_required",
                            "Upload media before planning an edit.",
                        )
                        db.commit()
                return
            feasible_duration_s = feasible_guided_duration_s(media)
            if feasible_duration_s < MIN_GUIDED_DURATION_S:
                with sync_session() as db:
                    locked = _locked_item(db, iid, ownership_epoch)
                    item = locked[0] if locked else None
                    current = parse_edit_proposal(item.edit_proposal) if item else None
                    if (
                        item
                        and current
                        and current.generation_attempt_id == attempt_id
                        and current.status == "analyzing"
                    ):
                        _fail(
                            item,
                            current,
                            "guided_edit_infeasible",
                            "This footage is only about "
                            f"{feasible_duration_s:.1f}s long — too short for a guided "
                            "edit. Add more media or use a shorter format.",
                        )
                        db.commit()
                return
            target_duration_s = adapt_target_duration_s(brief.duration_s, feasible_duration_s)
            digest = canonical_media_digest(media)

            with sync_session() as db:
                locked = _locked_item(db, iid, ownership_epoch)
                item = locked[0] if locked else None
                owner_id = locked[1] if locked else None
                current = parse_edit_proposal(item.edit_proposal) if item else None
                if (
                    item is None
                    or current is None
                    or current.generation_attempt_id != attempt_id
                    or current.status != "analyzing"
                ):
                    return
                current_assignments = [
                    dict(a)
                    for a in (item.clip_assignments or [])
                    if isinstance(a, dict) and a.get("gcs_path") and a.get("media_id")
                ]
                merged_assignments = _merge_analyzed_assignments(
                    current_assignments, analyzed_assignments
                )
                if merged_assignments is None:
                    _fail(
                        item,
                        current,
                        "proposal_stale",
                        "The uploaded media changed while planning.",
                    )
                    db.commit()
                    return
                if not media_generations_match_sync(clip_refs):
                    _fail(
                        item,
                        current,
                        "proposal_stale",
                        "The uploaded media changed while planning.",
                    )
                    db.commit()
                    return
                assert owner_id is not None
                fresh_pool = _pool_refs(db, item, owner_id)
                fresh_media = clip_refs + [
                    ref for ref in fresh_pool if ref.gcs_path not in clip_paths
                ]
                if canonical_media_digest(fresh_media) != digest:
                    _fail(
                        item,
                        current,
                        "proposal_stale",
                        "The uploaded media changed while planning.",
                    )
                    db.commit()
                    return
                item.clip_assignments = merged_assignments
                drafting = current.model_copy(
                    update={
                        "proposal_version": current.proposal_version + 1,
                        "status": "drafting",
                        "media_digest": digest,
                        "failure": None,
                    }
                )
                item.edit_proposal = drafting.model_dump(mode="json")
                db.commit()

            agent_media = [
                EditProposalMedia(
                    media_id=ref.media_id,
                    lane=ref.lane,
                    kind=ref.kind,
                    source_filename=ref.source_filename,
                    duration_s=ref.duration_s,
                    user_context=ref.user_context,
                    subject=str(ref.analysis.get("subject") or ""),
                    description=str(ref.analysis.get("description") or ""),
                    on_screen_text=str(ref.analysis.get("on_screen_text") or ""),
                    best_moments=list(ref.analysis.get("best_moments") or []),
                )
                for ref in media
            ]
            output = EditProposalAgent(default_client()).run(
                EditProposalAgentInput(
                    idea=idea,
                    theme=theme,
                    direction=brief.direction,
                    goal=brief.goal,
                    pace=brief.pace,
                    target_duration_s=target_duration_s,
                    media=agent_media,
                )
            )
            snapshot = EditProposalSnapshot(
                direction=brief.direction,
                goal=brief.goal,
                pace=brief.pace,
                duration_s=output.duration_s,
                title=output.title,
                media=media,
                story_beats=[
                    StoryBeat(
                        beat_id=str(uuid.uuid4()),
                        topic=beat.topic,
                        thought=beat.thought,
                        thought_source="ai_draft",
                        media_ids=beat.media_ids,
                        layout=beat.layout,
                        duration_s=beat.duration_s,
                    )
                    for beat in output.story_beats
                ],
            )
            with sync_session() as db:
                locked = _locked_item(db, iid, ownership_epoch)
                item = locked[0] if locked else None
                current = parse_edit_proposal(item.edit_proposal) if item else None
                if (
                    item is None
                    or current is None
                    or current.generation_attempt_id != attempt_id
                    or current.status != "drafting"
                    or current.media_digest != digest
                ):
                    return
                save_proposal_draft(
                    item,
                    expected_version=current.proposal_version,
                    snapshot=snapshot,
                )
                db.commit()
        except Retry:
            raise
        except SoftTimeLimitExceeded:
            # Celery will terminate the task after this signal. Persist a
            # creator-visible retry state first so the UI never polls an
            # abandoned analyzing/drafting attempt forever.
            with sync_session() as db:
                locked = _locked_item(db, iid, ownership_epoch)
                item = locked[0] if locked else None
                current = parse_edit_proposal(item.edit_proposal) if item else None
                if (
                    item
                    and current
                    and current.generation_attempt_id == attempt_id
                    and current.status in {"analyzing", "drafting"}
                ):
                    _fail(
                        item,
                        current,
                        "proposal_generation_timeout",
                        "Kria took too long to plan this edit. Try again.",
                    )
                    db.commit()
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("edit_proposal.draft_failed", item_id=item_id)
            with sync_session() as db:
                locked = _locked_item(db, iid, ownership_epoch)
                item = locked[0] if locked else None
                current = parse_edit_proposal(item.edit_proposal) if item else None
                if (
                    item
                    and current
                    and current.generation_attempt_id == attempt_id
                    and current.status in {"analyzing", "drafting"}
                ):
                    _fail(
                        item,
                        current,
                        "proposal_generation_failed",
                        "Kria couldn't plan this edit. Try again.",
                        detail=_exc_detail(exc),
                    )
                    db.commit()
