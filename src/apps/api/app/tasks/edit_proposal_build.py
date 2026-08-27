"""Background media analysis and proposal drafting for Plan edit."""

from __future__ import annotations

import math
import os
import tempfile
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path

import structlog
from billiard.exceptions import SoftTimeLimitExceeded
from celery.exceptions import MaxRetriesExceededError, Retry
from sqlalchemy import func, select

from app.database import sync_session
from app.models import ContentPlan, PlanItem, PlanItemAsset
from app.schemas.edit_proposal import (
    GUIDED_STORY_MIN_MOMENT_S,
    MAIN_CREATOR_FAIL_CLOSED,
    EditProposal,
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    ProposalFailure,
    StoryBeat,
    canonical_media_digest,
    parse_edit_proposal,
)
from app.services.content_plan_persona import load_owned_plan_persona_sync
from app.services.edit_direction_planner import clamp_fast_montage_target_duration_s
from app.services.edit_proposal_limits import (
    EDIT_PROPOSAL_TASK_HARD_TIME_LIMIT_S,
    EDIT_PROPOSAL_TASK_SOFT_TIME_LIMIT_S,
)
from app.services.edit_proposals import media_generations_match_sync, save_proposal_draft
from app.worker import celery_app

log = structlog.get_logger()

_BROKER_VISIBILITY_TIMEOUT_S = int(
    (celery_app.conf.broker_transport_options or {}).get("visibility_timeout", 1900)
)
if EDIT_PROPOSAL_TASK_HARD_TIME_LIMIT_S >= _BROKER_VISIBILITY_TIMEOUT_S:
    raise RuntimeError(
        "edit proposal hard time limit must stay below the broker visibility timeout"
    )
_TASK_LIMITS = {
    "soft_time_limit": EDIT_PROPOSAL_TASK_SOFT_TIME_LIMIT_S,
    "time_limit": EDIT_PROPOSAL_TASK_HARD_TIME_LIMIT_S,
}
_CLIP_ANALYSIS_CONCURRENCY = 3


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
# This is the absolute floor on the agent's Pydantic-validated input/output
# (EditProposalAgentInput.target_duration_s, EditProposalAgentOutput/
# EditProposalSnapshot.duration_s all carry ge=3) — it exists independently
# of guided_feasibility_threshold_s below and never changes.
MIN_GUIDED_DURATION_S = 3
# Images are not length-constrained by a source clip, so estimating "feasible"
# story length can't sum their real duration the way video works. Credit each
# image the guided_story per-media floor (guided_story.py _DIRECTION_POLICY
# "guided_story"."min_moment_s") rather than an unbounded amount.
_IMAGE_FEASIBLE_CREDIT_S = 1.4
# guided_story.py's own per-moment minimum for the "guided_story" direction.
# The shared schema constant keeps planning and rendering in lockstep without
# importing the pipeline. A video shorter than this can never be its own
# legible beat moment, so it earns zero credit below (P2-1, 2026-08-18
# adversarial review) — it was previously falling into the image branch and
# being credited as if it were a full _IMAGE_FEASIBLE_CREDIT_S image.


def _fast_story_beats(cuts: list[FastMontageCut]) -> list[StoryBeat]:
    """Build a compatibility beat projection for the exact fast-cut program.

    New fast plans render from ``fast_cuts``. The proposal envelope still keeps
    story beats for older readers and API shape compatibility, so group nearby
    cuts into short, text-free beats without changing their source windows.
    """

    beats: list[StoryBeat] = []
    for index in range(0, len(cuts), 4):
        group = cuts[index : index + 4]
        media_ids: list[str] = []
        for cut in group:
            if cut.media_id not in media_ids:
                media_ids.append(cut.media_id)
        group_duration = sum(cut.output_duration_s for cut in group)
        beats.append(
            StoryBeat(
                beat_id=f"fast-beat-{index // 4 + 1}",
                topic="Fast montage",
                thought="",
                thought_source="ai_draft",
                media_ids=media_ids,
                layout="fullscreen",
                duration_s=max(1.0, min(12.0, group_duration)),
            )
        )
    if not beats:
        raise ValueError("fast montage requires at least one cut")
    return beats


def feasible_guided_duration_s(media: list[MediaRef]) -> float:
    """Conservative, renderer-aware estimate of the story length the uploaded

    media can support. Videos contribute their own probed duration once — a
    beat can never be stretched past what was actually filmed (no
    slow-mo/loop) — but ONLY when that duration clears
    `GUIDED_STORY_MIN_MOMENT_S`; a video with no probed duration, a zero
    duration, or a duration too short to be its own legible moment
    contributes nothing (not the image credit). This is a pre-agent planning
    estimate — guided_story.py's `_source_window` / `_allocate_beat_durations`
    remain the exact, authoritative render-time feasibility check.
    """

    total = 0.0
    for ref in media:
        if ref.kind == "video":
            duration = float(ref.duration_s) if ref.duration_s else 0.0
            if duration >= GUIDED_STORY_MIN_MOMENT_S:
                total += duration
        else:
            total += _IMAGE_FEASIBLE_CREDIT_S
    return total


def guided_feasibility_threshold_s(media_count: int) -> float:
    """The minimum footage the renderer can plausibly turn into a guided story.

    Mirrors `_allocate_beat_durations`'s own hard requirement in
    guided_story.py (`beat_duration_s >= min_moment_s * len(refs)`, ~line 377)
    for a single beat holding every available source — `minimum_required_sources`
    (agents/edit_proposal.py) uses every source in one story when at most 3
    are available. Floored at `MIN_GUIDED_DURATION_S` so it never drops below
    the agent's own Pydantic input/output minimum. Below this,
    `feasible_guided_duration_s` never clears the bar, so `draft_edit_proposal`
    never calls the agent at all — the montage-fallback path handles it.
    """

    return max(
        MIN_GUIDED_DURATION_S,
        GUIDED_STORY_MIN_MOMENT_S * min(3, max(1, media_count)),
    )


def adapt_target_duration_s(brief_duration_s: int, feasible_s: float) -> int:
    """Clamp the brief's target to what the footage can actually support.

    Never exceeds the feasible estimate (floored, so the agent's target is
    never longer than real footage allows) and never exceeds the creator's
    requested duration. Callers must treat
    ``feasible_s < guided_feasibility_threshold_s(len(media))`` as infeasible
    before calling this — that threshold is always >= MIN_GUIDED_DURATION_S,
    so in correct use ``math.floor(feasible_s)`` alone already clears the
    ``max(MIN_GUIDED_DURATION_S, ...)`` floor below; it stays only as a
    defensive absolute floor matching the agent's Pydantic ge=3.
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
    from app.tasks.autoplace import (  # noqa: PLC0415
        ANALYSIS_VERSION,
        analysis_is_stale,
        analyze_pool_image,
        analyze_pool_video,
    )

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
    if analysis and kind == "video" and analysis_is_stale(analysis, kind=kind):
        analysis = {}  # rotation-naive pre-v6 row -- fall through to re-derive display dims
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
                analysis.setdefault("analysis_version", ANALYSIS_VERSION)
                analysis.setdefault("source", "probe_only")
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


def _analyze_clip_assignments(
    assignments: list[dict],
    pool_by_path: dict[str, MediaRef],
    *,
    item_id: uuid.UUID,
    attempt_id: str,
    ownership_epoch: int,
    on_complete: Callable[[dict, MediaRef], None] | None = None,
) -> list[tuple[dict, MediaRef]] | None:
    """Analyze raw clips three at a time while preserving assignment order.

    Pool assets have already been analyzed by their own per-asset Celery tasks.
    This only fans out synchronous source-clip work. ``None`` means the
    proposal attempt was superseded while work was in flight.
    """

    if not assignments:
        return []

    results: list[tuple[dict, MediaRef] | None] = [None] * len(assignments)
    next_index = 0
    futures: dict[Future[tuple[dict, MediaRef]], int] = {}

    executor = ThreadPoolExecutor(
        max_workers=min(_CLIP_ANALYSIS_CONCURRENCY, len(assignments)),
        thread_name_prefix="guided-clip-analysis",
    )
    completed_successfully = False
    try:

        def submit_next() -> bool:
            nonlocal next_index
            if next_index >= len(assignments):
                return False
            if not _attempt_is_active(item_id, attempt_id, ownership_epoch):
                return False
            future = executor.submit(
                _analyze_clip_assignment, assignments[next_index], pool_by_path
            )
            futures[future] = next_index
            next_index += 1
            return True

        while len(futures) < _CLIP_ANALYSIS_CONCURRENCY and next_index < len(assignments):
            if not submit_next():
                return None

        while futures:
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                index = futures.pop(future)
                # Propagate classified analysis exceptions to the caller so it
                # retains the existing retryable/permanent failure behaviour.
                result = future.result()
                results[index] = result
                if on_complete is not None:
                    on_complete(*result)

            while len(futures) < _CLIP_ANALYSIS_CONCURRENCY and next_index < len(assignments):
                if not submit_next():
                    return None
        completed_successfully = True
        return [result for result in results if result is not None]
    finally:
        # On a stale attempt or classified analysis failure, do not wait for
        # sibling Gemini calls to drain. Let the caller write its failure or
        # schedule the retry before the Celery hard limit, and cancel any
        # work that never started. A clean run still joins every worker.
        executor.shutdown(wait=completed_successfully, cancel_futures=not completed_successfully)


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


def _attempt_wants_auto_finalize(
    item_id: uuid.UUID,
    attempt_id: str,
    expected_ownership_epoch: int,
) -> bool:
    """Read approval_mode off the persisted envelope instead of trusting a

    task kwarg (P2-6, 2026-08-18 adversarial review). The auto-design intent
    is already durable on the proposal itself
    (begin_proposal_attempt(..., approval_mode="auto")), so a worker mid-
    deploy that predates auto-finalize entirely just never calls this
    function's caller's follow-up dispatch — it produces a plain draft
    (degraded: the creator manually approves) instead of either crashing on
    an unexpected kwarg (old worker, new producer) or silently never
    auto-finalizing (new worker, old producer that never set the kwarg).
    Cheap unlocked read, mirroring _attempt_is_active.
    """

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
            and current.approval_mode == "auto"
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


def _merge_analyzed_assignment(current: list[dict], analyzed: dict) -> list[dict] | None:
    """Checkpoint one result without overwriting concurrent creator edits.

    The generation check is intentionally stricter than the final batch merge:
    a result from an older object generation must never be written over a newer
    upload that kept the same media id/path.
    """

    identity = (str(analyzed.get("media_id") or ""), str(analyzed.get("gcs_path") or ""))
    if not all(identity):
        return None
    merged: list[dict] = []
    found = False
    for raw in current:
        entry = dict(raw)
        current_identity = (str(entry.get("media_id") or ""), str(entry.get("gcs_path") or ""))
        if current_identity == identity:
            current_generation = str(entry.get("generation") or "")
            analyzed_generation = str(analyzed.get("generation") or "")
            if current_generation and current_generation != analyzed_generation:
                return None
            for field in _ANALYSIS_FIELDS:
                if field in analyzed:
                    entry[field] = analyzed[field]
            found = True
        merged.append(entry)
    return merged if found else None


def _checkpoint_analyzed_assignment(
    item_id: uuid.UUID,
    attempt_id: str,
    ownership_epoch: int,
    analyzed: dict,
    _ref: MediaRef,
) -> None:
    """Persist each completed clip while the exact attempt still owns it."""

    with sync_session() as db:
        locked = _locked_item(db, item_id, ownership_epoch)
        item = locked[0] if locked else None
        current = parse_edit_proposal(item.edit_proposal) if item else None
        if (
            item is None
            or current is None
            or current.generation_attempt_id != attempt_id
            or current.status != "analyzing"
        ):
            return
        assignments = [
            dict(row)
            for row in (item.clip_assignments or [])
            if isinstance(row, dict) and row.get("gcs_path") and row.get("media_id")
        ]
        merged = _merge_analyzed_assignment(assignments, analyzed)
        if merged is None:
            return
        item.clip_assignments = merged
        db.commit()


def _dispatch_after_auto_design(
    iid: uuid.UUID, item_id: str, attempt_id: str, ownership_epoch: int
) -> None:
    """GUIDED_AUTO_DESIGN_ENABLED: dispatch after a draft attempt settles.

    Called unconditionally after _run_draft_attempt returns normally (i.e.
    every early-return failure path, plus the success path) — never after a
    re-raised Retry/SoftTimeLimitExceeded, since the attempt isn't actually
    over yet in those cases. Re-reads the proposal fresh (superseded-attempt
    fence via attempt_id) rather than threading final state through every
    return statement above:

      approved                              -> dispatch normally (bypass=False)
      failed, Main Creator-owned attempt     -> fail closed; its confirmed
                                                 strategy must not be discarded
      failed, zero pool assets, has clips    -> dispatch anyway (bypass=True),
                                                 mark design_fallback (legacy
                                                 montage render — never for an
                                                 item with pool assets, which
                                                 would silently drop them)
      anything else (analyzing/drafting/     -> nothing to do
      stale/failed-with-pool-assets/no clips)
    """

    from app.tasks.content_plan_build import dispatch_item_render_for  # noqa: PLC0415

    bypass = False
    with sync_session() as db:
        locked = _locked_item(db, iid, ownership_epoch)
        item = locked[0] if locked else None
        current = parse_edit_proposal(item.edit_proposal) if item else None
        if item is None or current is None or current.generation_attempt_id != attempt_id:
            return  # superseded by a newer attempt — nothing to do
        if current.status == "approved":
            bypass = False
        elif current.status == "failed" and current.design_fallback == MAIN_CREATOR_FAIL_CLOSED:
            log.warning(
                "edit_proposal.main_creator_fallback_refused",
                item_id=item_id,
                attempt_id=attempt_id,
                failure_code=current.failure.code if current.failure else "unknown",
            )
            return
        elif current.status == "failed" and not current.design_fallback:
            pool_count = db.execute(
                select(func.count())
                .select_from(PlanItemAsset)
                .where(PlanItemAsset.plan_item_id == item.id)
            ).scalar_one()
            if pool_count > 0 or not (item.clip_gcs_paths or []):
                return
            failure_code = current.failure.code if current.failure else "unknown"
            fallback = current.model_copy(
                update={
                    "proposal_version": current.proposal_version + 1,
                    "design_fallback": failure_code,
                }
            )
            item.edit_proposal = fallback.model_dump(mode="json")
            db.commit()
            bypass = True
        else:
            return

    dispatch_kwargs = {"bypass_guided_edit_gate": bypass}
    dispatch_kwargs["creator_guided_attempt_id"] = attempt_id
    result = dispatch_item_render_for(item_id, ownership_epoch, **dispatch_kwargs)
    if result.outcome not in {"dispatched", "already_active"}:
        # The proposal is already committed (approved, or failed+design_fallback)
        # — leave it there. The next manual Generate click dispatches directly
        # (approved reads identically regardless of approval_mode) or
        # re-triggers auto-design (failed), never wedged.
        log.warning(
            "edit_proposal.auto_finalize_dispatch_failed",
            item_id=item_id,
            outcome=result.outcome,
            fallback=bypass,
        )


@celery_app.task(
    bind=True,
    name="app.tasks.edit_proposal_build.draft_edit_proposal",
    max_retries=40,
    default_retry_delay=15,
    **_TASK_LIMITS,
)
def draft_edit_proposal(
    self,  # noqa: ANN001
    item_id: str,
    attempt_id: str,
    expected_ownership_epoch: int,
) -> None:
    """Analyze media, draft the story, persist it.

    No auto_finalize kwarg (P2-6, 2026-08-18 adversarial review — a task
    kwarg is a Celery deploy-skew hazard: an old worker consuming a message
    from a new producer, or vice versa, either crashes on an unknown kwarg or
    silently drops it). The GUIDED_AUTO_DESIGN_ENABLED intent is read
    straight off the persisted envelope instead
    (_attempt_wants_auto_finalize — approval_mode="auto", set by
    begin_proposal_attempt at reservation time): a successful draft is
    auto-approved and the render dispatched — AFTER the approval commits,
    never while holding the PlanItem row lock — or, on a drafting failure
    with zero registered pool assets, falls back to a legacy clip render
    instead (_dispatch_after_auto_design). On any other failure the proposal
    simply stays "failed" — the next manual Generate click re-triggers
    auto-design, never wedged.
    """

    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    try:
        iid = uuid.UUID(item_id)
        ownership_epoch = int(expected_ownership_epoch)
    except (TypeError, ValueError):
        return

    auto_finalize = _attempt_wants_auto_finalize(iid, attempt_id, ownership_epoch)

    with pipeline_trace_for(item_id):
        _run_draft_attempt(self, iid, item_id, attempt_id, ownership_epoch, auto_finalize)

    if auto_finalize:
        _dispatch_after_auto_design(iid, item_id, attempt_id, ownership_epoch)


def _run_draft_attempt(
    self,  # noqa: ANN001
    iid: uuid.UUID,
    item_id: str,
    attempt_id: str,
    ownership_epoch: int,
    auto_finalize: bool,
) -> None:
    """The actual analyze -> draft -> (auto-approve) body of one attempt.

    Every early return below is a normal, handled outcome (the failure/stale
    state is already persisted) — the caller always proceeds to
    _dispatch_after_auto_design when auto_finalize is set, regardless of
    which return statement fired. Only Retry/SoftTimeLimitExceeded propagate
    out (the attempt genuinely isn't over: Celery is rescheduling it, or
    killing the worker process).
    """

    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import TerminalError  # noqa: PLC0415
    from app.agents.edit_proposal import (  # noqa: PLC0415
        EditProposalAgent,
        EditProposalAgentInput,
        EditProposalMedia,
    )
    from app.services.edit_direction_planner import (  # noqa: PLC0415
        deterministic_fast_cuts,
        deterministic_guided_beats,
    )
    from app.services.edit_proposals import approve_proposal  # noqa: PLC0415

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
            if proposal.planning_started_at is None:
                proposal = proposal.model_copy(update={"planning_started_at": datetime.now(UTC)})
                item.edit_proposal = proposal.model_dump(mode="json")
                db.commit()

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
        try:
            analyzed_results = _analyze_clip_assignments(
                assignments,
                pool_by_path,
                item_id=iid,
                attempt_id=attempt_id,
                ownership_epoch=ownership_epoch,
                on_complete=lambda analyzed, ref: _checkpoint_analyzed_assignment(
                    iid, attempt_id, ownership_epoch, analyzed, ref
                ),
            )
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
        if analyzed_results is None:
            return
        analyzed_assignments = [entry for entry, _ref in analyzed_results]
        clip_refs = [ref for _entry, ref in analyzed_results]
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
        feasibility_threshold_s = guided_feasibility_threshold_s(len(media))
        if feasible_duration_s < feasibility_threshold_s:
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
        if brief.direction == "fast_montage":
            try:
                target_duration_s = clamp_fast_montage_target_duration_s(
                    media, target_duration_s, brief.mixed_media_timing
                )
            except ValueError as exc:
                # A typed mixed-media request can pass the generic guided
                # feasibility floor while still lacking the minimum source
                # capacity for its per-kind holds. Keep that actionable
                # outcome distinct from an unexpected proposal failure.
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
                            "The requested photo/video pacing needs at least 3s of usable "
                            "source footage. Add another photo or video, or choose a "
                            "different edit direction.",
                            detail=_exc_detail(exc),
                        )
                        db.commit()
                return
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
            fresh_media = clip_refs + [ref for ref in fresh_pool if ref.gcs_path not in clip_paths]
            if canonical_media_digest(fresh_media) != digest:
                _fail(
                    item,
                    current,
                    "proposal_stale",
                    "The uploaded media changed while planning.",
                )
                db.commit()
                return
            from app.services.speech_cleanup import (  # noqa: PLC0415
                cleanup_inputs,
                reconcile_item_policy_change,
            )

            previous_speech_inputs = cleanup_inputs(item)
            item.clip_assignments = merged_assignments
            reconcile_item_policy_change(item, previous_speech_inputs)
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
        fallback_used = False
        try:
            output = EditProposalAgent(default_client()).run(
                EditProposalAgentInput(
                    idea=idea,
                    theme=theme,
                    direction=brief.direction,
                    goal=brief.goal,
                    creator_request=brief.creator_request,
                    pace=brief.pace,
                    target_duration_s=target_duration_s,
                    mixed_media_timing=brief.mixed_media_timing,
                    media=agent_media,
                )
            )
        except TerminalError as exc:
            if brief.direction == "text_explainer":
                raise
            fallback_used = True
            output = None
            log.warning(
                "edit_proposal.deterministic_fallback",
                item_id=item_id,
                direction=brief.direction,
                error=str(exc),
            )
        # EditProposalAgent.parse()'s +/-5s tolerance checks output.duration_s
        # against target_duration_s, NOT against real footage — with a small
        # target (e.g. the MIN_GUIDED_DURATION_S=3 floor) that tolerance
        # window alone could still accept an output nobody validated against
        # the true footage cap (P2-1a, 2026-08-18 adversarial review). Reject
        # rather than silently rewrite per-beat durations the agent already
        # sized for a specific total.
        if output is not None and output.duration_s > math.floor(feasible_duration_s):
            with sync_session() as db:
                locked = _locked_item(db, iid, ownership_epoch)
                item = locked[0] if locked else None
                current = parse_edit_proposal(item.edit_proposal) if item else None
                if (
                    item
                    and current
                    and current.generation_attempt_id == attempt_id
                    and current.status == "drafting"
                ):
                    _fail(
                        item,
                        current,
                        "guided_edit_infeasible",
                        "Kria's draft ran longer than the actual footage allows. "
                        "Try again or add more media.",
                        detail=(
                            f"output.duration_s={output.duration_s} exceeds "
                            f"feasible={feasible_duration_s:.2f} "
                            f"(target={target_duration_s})"
                        ),
                    )
                    db.commit()
            return
        if output is None and brief.direction == "fast_montage":
            fallback_cuts = deterministic_fast_cuts(
                media, target_duration_s, brief.mixed_media_timing
            )
            fallback_beats = _fast_story_beats(fallback_cuts)
        else:
            fallback_cuts = None
            fallback_beats = (
                deterministic_guided_beats(media, target_duration_s) if output is None else None
            )
        snapshot = EditProposalSnapshot(
            direction=brief.direction,
            goal=brief.goal,
            pace=brief.pace,
            duration_s=output.duration_s if output is not None else target_duration_s,
            title=output.title if output is not None else "A few moments",
            media=media,
            story_beats=(
                [
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
                ]
                if output is not None and brief.direction != "fast_montage"
                else _fast_story_beats(output.fast_cuts or [])
                if output is not None
                else fallback_beats
            ),
            fast_cuts=(
                [cut.model_dump(mode="json") for cut in output.fast_cuts]
                if output is not None and brief.direction == "fast_montage" and output.fast_cuts
                else fallback_cuts
            ),
            mixed_media_timing=brief.mixed_media_timing,
            output_orientation=brief.output_orientation,
        )
        if fallback_used:
            # Never auto-approve a deterministic recovery that the strict
            # renderer cannot compile from the complete accepted media set.
            from app.pipeline.guided_story import validate_proposal_timing  # noqa: PLC0415

            validate_proposal_timing(snapshot)
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
            drafted = save_proposal_draft(
                item,
                expected_version=current.proposal_version,
                snapshot=snapshot,
            )
            if auto_finalize:
                # Dispatch happens in _dispatch_after_auto_design, called by
                # draft_edit_proposal only after this function returns and
                # the lock above is released — never while holding it.
                approve_proposal(item, expected_version=drafted.proposal_version)
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
