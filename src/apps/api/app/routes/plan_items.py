"""Plan-item endpoints + shared item serialization (content-plan Phase 4).

PATCH /plan-items/{id} — hand-edit a plan item (theme / idea / filming_suggestion).

Also the home of `derive_item_status` + `plan_item_response`, used here and by
content_plans.py. Live render state is DERIVED from the linked Job.status at read
time (plan T2): `item_status` on the row only ever holds `idea` | `awaiting_clips`;
generating / ready / failed come from the Job so a reaper-killed job can never
leave an item stuck "generating" forever.
"""

from __future__ import annotations

import asyncio
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import storage
from app.agents.music_matcher import _sanitize_text
from app.auth import CurrentUser
from app.database import get_db
from app.models import ContentPlan, Job, Persona, PlanItem
from app.routes.generative_jobs import (
    ChangeStyleRequest,
    RetextRequest,
    SetIntroSizeRequest,
    SwapSongRequest,
    TimelineEditRequest,
    TimelineResponse,
    dispatch_change_style,
    dispatch_edit_timeline,
    dispatch_get_timeline,
    dispatch_reset_timeline,
    dispatch_retext,
    dispatch_set_intro_size,
    dispatch_swap_song,
)

log = structlog.get_logger()
router = APIRouter()

# Themed plan uploads land under the persistent `users/` prefix (NOT swept by the
# 24h GCS delete rule). Allowlisted in admin_music._ALLOWED_CLIP_PREFIXES.
_MAX_CLIPS_PER_ITEM = 20
_MAX_BYTES_PER_FILE = 4 * 1024 * 1024 * 1024  # 4GB
_ALLOWED_CONTENT_TYPES = {"video/mp4", "video/quicktime"}

# Job.status buckets (mode="content_plan" reuses the generative variant states).
_JOB_READY = {"variants_ready", "variants_ready_partial", "done", "clips_ready"}
_JOB_FAILED = {
    "variants_failed",
    "matching_failed",
    "no_labeled_tracks",
    "processing_failed",
    "posting_failed",
    "cancelled",
}


def derive_item_status(item: PlanItem) -> str:
    """idea | awaiting_clips | generating | ready | failed — derived, never stored."""
    job = item.current_job
    if job is None:
        # No job minted yet: row state is the source of truth (idea/awaiting_clips).
        return item.item_status
    if job.status in _JOB_READY:
        return "ready"
    if job.status in _JOB_FAILED:
        return "failed"
    return "generating"


class FilmingShotResponse(BaseModel):
    """One shot from the plan item's filming guide.

    All fields default to safe values so a hand-corrupted or legacy JSONB row
    with missing keys never 500s the read path.
    """

    shot_id: str | None = None  # stable server-assigned uuid; null for legacy pre-0052 rows
    what: str = ""
    how: str = ""
    duration_s: int = 1  # matches MIN_SHOT_DURATION_S; 0 would render as confusing "0s" badge


class ClipAssignmentResponse(BaseModel):
    """One clip assignment in the response (mirrors the DB JSONB shape)."""

    gcs_path: str
    shot_id: str | None = None  # null = extra-footage pool
    # Optional creator context about the clip; "" when unset.
    user_note: str = ""
    # True = the footage-pool matcher placed this clip (provisional chip in the
    # UI; conformance suppressed until the user keeps/swaps/replaces it).
    machine_matched: bool = False


class PlanItemResponse(BaseModel):
    id: str
    day_index: int
    theme: str
    idea: str
    filming_suggestion: str | None
    # The AI's "why this works", surfaced read-only in the dashboard.
    rationale: str | None
    # Structured shot list (2–4 shots). Always a list; empty for legacy items
    # whose plans predate this field (frontend falls back to filming_suggestion).
    filming_guide: list[FilmingShotResponse]
    clip_gcs_paths: list[str]
    # Per-shot clip assignments. Shape: [{gcs_path, shot_id}]; shot_id=null = pool.
    # Populated since migration 0052; empty list for items with no clips yet.
    clip_assignments: list[ClipAssignmentResponse] = []
    status: str
    current_job_id: str | None
    user_edited: bool
    # Creator Agent M4: instruction level from the owning user's style entity.
    # Drives the instructed/uninstructed upload split on the item page:
    #   "full" or "light" → single-file replace mode when filming_guide is present
    #   "none" → keep existing bulk-append behaviour unchanged
    # Default "full": applies to items whose plan predates M1 or when style is absent.
    instruction_level: str = "full"
    # ConformanceFeedbackAgent verdict (best-effort, display-only). NULL until the
    # agent runs (flag on + clip attached). Never blocks Generate.
    conformance: dict | None = None
    # Persona content mode (direction fork, 2026-06-11): drives the film-card
    # header copy — "HOW TO FILM THIS" (create_new/legacy) vs "WHAT TO LOOK FOR"
    # (existing_footage) vs "FIND IT OR FILM IT" (mixed). Populated on the item
    # GET (the page's poll path); other responses default to create_new.
    content_mode: str = "create_new"


def plan_item_response(
    item: PlanItem,
    *,
    instruction_level: str = "full",
    content_mode: str = "create_new",
) -> PlanItemResponse:
    # Tolerate missing keys in individual JSONB shots — each shot is constructed
    # via .get() so a hand-corrupted row or a migration-era partial row never raises.
    shots = [
        FilmingShotResponse(
            shot_id=s.get("shot_id"),  # None for pre-0052 rows (backfilled by migration)
            what=s.get("what", ""),
            how=s.get("how", ""),
            duration_s=s.get("duration_s", 1),  # 1 = MIN_SHOT_DURATION_S; 0 renders as "0s" badge
        )
        for s in (item.filming_guide or [])
        if isinstance(s, dict)
    ]

    # Read-time reconciliation (D15): any assignment whose shot_id is no longer
    # present in the current filming_guide is presented as pool (shot_id=null).
    # This handles the case where the guide was rerolled after clips were attached;
    # the assignment becomes visible extra footage rather than a ghost.
    live_shot_ids = {s.shot_id for s in shots if s.shot_id is not None}
    raw_assignments = item.clip_assignments or []
    reconciled_assignments = [
        ClipAssignmentResponse(
            gcs_path=a.get("gcs_path", ""),
            shot_id=a.get("shot_id") if a.get("shot_id") in live_shot_ids else None,
            user_note=str(a.get("user_note") or ""),
            machine_matched=bool(a.get("machine_matched")),
        )
        for a in raw_assignments
        if isinstance(a, dict) and a.get("gcs_path")
    ]

    return PlanItemResponse(
        id=str(item.id),
        day_index=item.day_index,
        theme=item.theme,
        idea=item.idea,
        filming_suggestion=item.filming_suggestion,
        rationale=item.rationale,
        filming_guide=shots,
        clip_gcs_paths=list(item.clip_gcs_paths or []),
        clip_assignments=reconciled_assignments,
        status=derive_item_status(item),
        current_job_id=str(item.current_job_id) if item.current_job_id else None,
        user_edited=item.user_edited,
        instruction_level=instruction_level,
        conformance=item.conformance,
        content_mode=content_mode
        if content_mode in ("existing_footage", "create_new", "mixed")
        else "create_new",
    )


async def _get_content_mode(item: PlanItem, db: AsyncSession) -> str:
    """Persona content_mode via item → plan → persona JSONB; default create_new."""
    try:
        plan = await db.get(ContentPlan, item.content_plan_id)
        if plan is None:
            return "create_new"
        persona = await db.get(Persona, plan.persona_id)
        if persona is None or not isinstance(persona.persona, dict):
            return "create_new"
        return str(persona.persona.get("content_mode") or "create_new")
    except Exception:  # noqa: BLE001
        return "create_new"


async def _get_instruction_level(item: PlanItem, db: AsyncSession) -> str:
    """Read instruction_level from the owning user's personas.style JSONB.

    Null-safe chain: item → ContentPlan → Persona → style → instruction_level.
    Any missing link → default "full".
    """
    try:
        plan = await db.get(ContentPlan, item.content_plan_id)
        if plan is None:
            return "full"
        persona = await db.get(Persona, plan.persona_id)
        if persona is None:
            return "full"
        style = persona.style or {}
        level = str(style.get("instruction_level", "full") or "full")
        return level if level in ("full", "light", "none") else "full"
    except Exception:  # noqa: BLE001
        return "full"


@router.get("/{item_id}", response_model=PlanItemResponse)
async def get_plan_item(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    item = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(item, db)
    content_mode = await _get_content_mode(item, db)
    return plan_item_response(item, instruction_level=instruction_level, content_mode=content_mode)


class PlanItemEdit(BaseModel):
    theme: str | None = None
    idea: str | None = None
    filming_suggestion: str | None = None


@router.patch("/{item_id}", response_model=PlanItemResponse)
async def edit_plan_item(
    item_id: str,
    edit: PlanItemEdit,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    item = await _load_owned_item(item_id, user.id, db)

    updates = edit.model_dump(exclude_none=True)
    if "theme" in updates:
        item.theme = _sanitize_text(updates["theme"]) or item.theme
    if "idea" in updates:
        item.idea = _sanitize_text(updates["idea"]) or item.idea
    if "filming_suggestion" in updates:
        item.filming_suggestion = _sanitize_text(updates["filming_suggestion"]) or None
    if updates:
        item.user_edited = True
    await db.commit()
    # Reload with current_job eager-loaded (commit expired it) before serializing.
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


# ── Themed uploads + per-item generation ──────────────────────────────────────


async def _load_owned_item(item_id: str, user_id: uuid.UUID, db: AsyncSession) -> PlanItem:
    try:
        iid = uuid.UUID(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc
    # Eager-load current_job: plan_item_response → derive_item_status reads the
    # relationship, and a bare db.get() leaves it lazy → MissingGreenlet 500 on
    # the async session once an item has a linked job (mirrors the list endpoint's
    # selectinload in content_plans.py).
    item = (
        await db.execute(
            select(PlanItem).where(PlanItem.id == iid).options(selectinload(PlanItem.current_job))
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")
    plan = await db.get(ContentPlan, item.content_plan_id)
    if plan is None or plan.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")
    return item


class UploadFile(BaseModel):
    filename: str
    content_type: str
    file_size_bytes: int


class UploadUrlsBody(BaseModel):
    files: list[UploadFile]


class UploadUrlItem(BaseModel):
    upload_url: str
    gcs_path: str


class UploadUrlsResponse(BaseModel):
    urls: list[UploadUrlItem]


@router.post("/{item_id}/upload-urls", response_model=UploadUrlsResponse)
async def create_upload_urls(
    item_id: str,
    body: UploadUrlsBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UploadUrlsResponse:
    """Signed PUT URLs for themed clips, under the persistent users/ prefix."""
    item = await _load_owned_item(item_id, user.id, db)
    if not body.files or len(body.files) > _MAX_CLIPS_PER_ITEM:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provide 1-{_MAX_CLIPS_PER_ITEM} files",
        )
    urls: list[UploadUrlItem] = []
    for f in body.files:
        if f.content_type not in _ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported content type: {f.content_type}",
            )
        if f.file_size_bytes <= 0 or f.file_size_bytes > _MAX_BYTES_PER_FILE:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bad file size")
        # Prefix a uuid so two uploads with the same filename don't collide.
        safe_name = f"{uuid.uuid4().hex}-{f.filename.split('/')[-1]}"
        url, gcs_path = storage.presigned_put_url_for_plan_item(
            user_id=str(user.id),
            plan_item_id=str(item.id),
            filename=safe_name,
            content_type=f.content_type,
        )
        urls.append(UploadUrlItem(upload_url=url, gcs_path=gcs_path))
    return UploadUrlsResponse(urls=urls)


_MAX_NOTE_CHARS = 200


class ClipAssignmentBody(BaseModel):
    """One clip assignment sent from the frontend."""

    gcs_path: str
    shot_id: str | None = None  # null = extra-footage pool
    # Optional creator context ("famous vegan restaurant in Buenos Aires").
    # UNTRUSTED free-text: length-capped here, sanitized + DATA-framed at every
    # prompt boundary that consumes it.
    user_note: str = ""


class AttachClipsBody(BaseModel):
    clip_gcs_paths: list[str]
    # Optional per-shot assignments (shot-slot uploader). When absent the whole
    # batch is treated as pool (legacy / uninstructed callers are unaffected).
    assignments: list[ClipAssignmentBody] | None = None


@router.post("/{item_id}/clips", response_model=PlanItemResponse)
async def attach_clips(
    item_id: str,
    body: AttachClipsBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Record uploaded clip paths on the item (validated to the users/ prefix).

    Assignment semantics (shot-slot uploader, D16):
      - body.assignments present → validate shot_ids + derive clip_gcs_paths via set_item_clips
      - body.assignments absent  → treat body.clip_gcs_paths as pool (legacy callers)

    D7: nulls item.conformance before dispatching re-analysis so the panel can
    never describe replaced footage. If re-analysis fails, the panel is absent,
    not stale.
    """
    from app.services.plan_clips import (  # noqa: PLC0415
        ClipAssignment,
        ClipAssignmentError,
        set_item_clips,
    )

    item = await _load_owned_item(item_id, user.id, db)
    expected_prefix = f"users/{user.id}/plan/{item.id}/"

    if body.assignments is not None:
        # Shot-slot uploader path: validate prefix, then validate shot_ids.
        for a in body.assignments:
            if not a.gcs_path.startswith(expected_prefix):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Clip path outside this plan item's upload prefix",
                )

        # Build set of live shot_ids from the item's filming_guide.
        live_shot_ids: set[str] = {
            s["shot_id"]
            for s in (item.filming_guide or [])
            if isinstance(s, dict) and s.get("shot_id")
        }

        for a in body.assignments:
            if a.shot_id is not None and a.shot_id not in live_shot_ids:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unknown shot_id: {a.shot_id}",
                )

        assignments = [
            ClipAssignment(
                gcs_path=a.gcs_path,
                shot_id=a.shot_id,
                user_note=(a.user_note or "")[:_MAX_NOTE_CHARS],
            )
            for a in body.assignments
        ]
    else:
        # Legacy / uninstructed path: all clips go to pool.
        for p in body.clip_gcs_paths:
            if not p.startswith(expected_prefix):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Clip path outside this plan item's upload prefix",
                )
        assignments = [ClipAssignment(gcs_path=p, shot_id=None) for p in body.clip_gcs_paths]

    try:
        set_item_clips(item, assignments)
    except ClipAssignmentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # D7: null conformance so the panel can never describe replaced footage.
    item.conformance = None

    await db.commit()
    # Fire-and-forget conformance analysis (best-effort, never blocks this response).
    from app.tasks.conformance_build import analyze_item_conformance  # noqa: PLC0415

    analyze_item_conformance.delay(str(item.id))
    # Reload with current_job eager-loaded (commit expired it) before serializing.
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


class ClipNoteBody(BaseModel):
    gcs_path: str
    user_note: str = ""


@router.patch("/{item_id}/clips/note", response_model=PlanItemResponse)
async def set_clip_note(
    item_id: str,
    body: ClipNoteBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Set/clear the creator's context note on one attached clip.

    Editing a note counts as the user touching the slot, so it also clears
    machine_matched. The conformance verdict is reset to a carry-over stub
    (contested flag only) and re-analysis is dispatched — the panel shows the
    checking state, never a stale verdict, while the judge re-reads the clip
    with the new context.
    """
    item = await _load_owned_item(item_id, user.id, db)
    note = (body.user_note or "")[:_MAX_NOTE_CHARS]

    assignments = list(item.clip_assignments or [])
    hit = False
    updated = []
    for a in assignments:
        entry = dict(a) if isinstance(a, dict) else {}
        if entry.get("gcs_path") == body.gcs_path:
            entry["user_note"] = note
            entry["machine_matched"] = False
            hit = True
        updated.append(entry)
    if not hit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such clip on this item"
        )
    item.clip_assignments = updated

    # Carry only the contested flag through the re-run (suppression memory);
    # the old verdict itself must never render while the judge re-reads.
    prev = item.conformance or {}
    item.conformance = {"contested": True} if prev.get("contested") else None

    await db.commit()
    from app.tasks.conformance_build import analyze_item_conformance  # noqa: PLC0415

    analyze_item_conformance.delay(str(item.id))
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


@router.post("/{item_id}/conformance/dismiss", response_model=PlanItemResponse)
async def dismiss_conformance(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """'Hide this read' — persist the dismissal so the verdict never re-renders
    for this footage (a fresh attach nulls conformance and starts over)."""
    item = await _load_owned_item(item_id, user.id, db)
    if item.conformance:
        item.conformance = {**item.conformance, "dismissed": True}
        await db.commit()
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


@router.post("/{item_id}/conformance/contest", response_model=PlanItemResponse)
async def contest_conformance(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """'Looks wrong? Tell Nova' — mark the verdict contested. From here on,
    only high-confidence (≥0.8) verdicts may render on this footage."""
    item = await _load_owned_item(item_id, user.id, db)
    if item.conformance:
        item.conformance = {**item.conformance, "contested": True}
        await db.commit()
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


# ── Ask Nova (per-item filming advisor) ───────────────────────────────────────


class AdvisorTurnBody(BaseModel):
    answer: str = ""
    # Full conversation so far: [{role: "agent"|"user", content: str}] — the
    # advisor is stateless per turn (same contract as /personas/agent/turn).
    prior_turns: list[dict] = []


class AdvisorTurnResponse(BaseModel):
    reply: str
    suggestions: list[str] = []
    # Non-empty when the agent proposes re-reading a clip with this distilled
    # creator context; the frontend asks consent then PATCHes the clip note.
    suggested_note: str = ""


@router.post("/{item_id}/agent/turn", response_model=AdvisorTurnResponse)
async def plan_item_advisor_turn(
    item_id: str,
    body: AdvisorTurnBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AdvisorTurnResponse:
    """One "Ask Nova" turn on this item: which clip fits, what to film instead,
    or contesting the brief read. Read-only — advice, never writes."""
    from app.config import settings  # noqa: PLC0415

    if not settings.plan_item_advisor_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="advisor_not_enabled")

    item = await _load_owned_item(item_id, user.id, db)

    # Persona context (summary + content_mode), null-safe.
    persona_summary = ""
    content_mode = "create_new"
    plan = await db.get(ContentPlan, item.content_plan_id)
    if plan is not None:
        persona_row = await db.get(Persona, plan.persona_id)
        if persona_row is not None and isinstance(persona_row.persona, dict):
            persona_summary = str(persona_row.persona.get("summary") or "")
            content_mode = str(persona_row.persona.get("content_mode") or "create_new")

    # Clips block: filename (uuid prefix stripped), slot label, creator note.
    shot_label_by_id = {
        s.get("shot_id"): f"shot {i + 1}"
        for i, s in enumerate(item.filming_guide or [])
        if isinstance(s, dict) and s.get("shot_id")
    }
    clips = []
    for a in item.clip_assignments or []:
        if not isinstance(a, dict) or not a.get("gcs_path"):
            continue
        raw_name = str(a["gcs_path"]).rsplit("/", 1)[-1]
        filename = raw_name.split("-", 1)[1] if "-" in raw_name else raw_name
        clips.append(
            {
                "filename": filename,
                "shot_label": shot_label_by_id.get(a.get("shot_id"), "extra footage"),
                "user_note": str(a.get("user_note") or ""),
            }
        )

    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents.interviewer_agent import ConversationTurn  # noqa: PLC0415
    from app.agents.plan_item_advisor import (  # noqa: PLC0415
        PlanItemAdvisorAgent,
        PlanItemAdvisorInput,
    )

    turns = [
        ConversationTurn(role=str(t.get("role", "user")), content=str(t.get("content", "")))
        for t in body.prior_turns
        if isinstance(t, dict) and t.get("content")
    ]
    if body.answer.strip():
        turns.append(ConversationTurn(role="user", content=body.answer.strip()))

    agent_input = PlanItemAdvisorInput(
        turns=turns,
        theme=str(item.theme or ""),
        idea=str(item.idea or ""),
        edit_format=str(getattr(item, "edit_format", "") or "montage"),
        filming_guide=list(item.filming_guide or []),
        clips=clips,
        conformance=item.conformance if isinstance(item.conformance, dict) else None,
        job_phase=derive_item_status(item),
        persona_summary=persona_summary,
        content_mode=content_mode,
    )

    try:
        result = await asyncio.to_thread(PlanItemAdvisorAgent(default_client()).run, agent_input)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_item_advisor.failed", item_id=item_id, error=str(exc)[:300])
        return AdvisorTurnResponse(
            reply=(
                "I couldn't think that through just now — try asking again. "
                "You can always generate with what you have."
            ),
            suggestions=["Which clip fits shot 1?", "What should I film instead?"],
        )

    return AdvisorTurnResponse(
        reply=result.reply,
        suggestions=result.suggestions,
        suggested_note=result.suggested_note,
    )


@router.post("/{item_id}/generate", response_model=PlanItemResponse)
async def generate_item(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Enqueue a generative render for this item's themed clips (≥1 required)."""
    item = await _load_owned_item(item_id, user.id, db)
    if not (item.clip_gcs_paths or []):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Upload at least one clip before generating",
        )
    # Idempotency guard (dogfood: double-clicking Generate minted two render
    # jobs). The Job is created async by the task, so also reject while the
    # row state says a dispatch is pending/in flight.
    if derive_item_status(item) == "generating":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A render is already in progress for this item",
        )
    from app.tasks.content_plan_build import generate_plan_item_videos  # noqa: PLC0415

    generate_plan_item_videos.delay(str(item.id))
    # current_job_id is set by the task, not synchronously here; reload with the
    # relationship eager-loaded so serialization never lazy-loads on the session.
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


# ── Per-variant editing (swap song / edit text / change style) ────────────────
# The render job behind a plan item is a generative-mode Job, so each variant can
# be re-rendered exactly like a public generative edit. These endpoints add only
# ownership enforcement (`_load_owned_item`) + job resolution on top of the shared
# validate-and-dispatch helpers in `routes/generative_jobs.py` — the validation
# rules and the `regenerate_generative_variant` dispatch stay single-sourced there.
# Mutation is reachable ONLY here (authenticated, per-user), never on the public
# unauthenticated `/generative-jobs` surface.


async def _owned_item_render_job(item_id: str, user_id: uuid.UUID, db: AsyncSession) -> Job:
    """Load the user-owned item and return its current render Job (404 if none yet)."""
    item = await _load_owned_item(item_id, user_id, db)
    job = item.current_job
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No render to edit yet")
    return job


@router.post("/{item_id}/variants/{variant_id}/swap-song", response_model=PlanItemResponse)
async def swap_item_song(
    item_id: str,
    variant_id: str,
    req: SwapSongRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Re-render one of this item's variants against a different library song."""
    job = await _owned_item_render_job(item_id, user.id, db)
    await dispatch_swap_song(job, variant_id, new_track_id=req.new_track_id, db=db)
    log.info("plan_item_swap_song", item_id=item_id, variant_id=variant_id)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post("/{item_id}/variants/{variant_id}/retext", response_model=PlanItemResponse)
async def retext_item(
    item_id: str,
    variant_id: str,
    req: RetextRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Re-render one of this item's variants with new intro text, or remove it."""
    job = await _owned_item_render_job(item_id, user.id, db)
    dispatch_retext(job, variant_id, text=req.text, remove=req.remove)
    log.info("plan_item_retext", item_id=item_id, variant_id=variant_id, remove=req.remove)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post("/{item_id}/variants/{variant_id}/change-style", response_model=PlanItemResponse)
async def change_item_style(
    item_id: str,
    variant_id: str,
    req: ChangeStyleRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Re-render one of this item's variants with a different curated text style set."""
    job = await _owned_item_render_job(item_id, user.id, db)
    dispatch_change_style(job, variant_id, style_set_id=req.style_set_id)
    log.info("plan_item_change_style", item_id=item_id, variant_id=variant_id)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post("/{item_id}/variants/{variant_id}/intro-size", response_model=PlanItemResponse)
async def set_item_intro_size(
    item_id: str,
    variant_id: str,
    req: SetIntroSizeRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Re-render one of this item's variants with a user-pinned AI-intro font size."""
    job = await _owned_item_render_job(item_id, user.id, db)
    dispatch_set_intro_size(job, variant_id, text_size_px=req.text_size_px)
    log.info(
        "plan_item_set_intro_size", item_id=item_id, variant_id=variant_id, px=req.text_size_px
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.get("/{item_id}/variants/{variant_id}/timeline", response_model=TimelineResponse)
async def get_item_timeline(
    item_id: str,
    variant_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TimelineResponse:
    """The effective clip timeline of one of this item's variants (+ clip pool)."""
    job = await _owned_item_render_job(item_id, user.id, db)
    return TimelineResponse(**dispatch_get_timeline(job, variant_id))


@router.post("/{item_id}/variants/{variant_id}/timeline", response_model=PlanItemResponse)
async def edit_item_timeline(
    item_id: str,
    variant_id: str,
    req: TimelineEditRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Persist a user-edited clip timeline for one of this item's variants + re-render."""
    job = await _owned_item_render_job(item_id, user.id, db)
    await dispatch_edit_timeline(job, variant_id, req, db=db)
    log.info("plan_item_edit_timeline", item_id=item_id, variant_id=variant_id)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.delete("/{item_id}/variants/{variant_id}/timeline", response_model=PlanItemResponse)
async def reset_item_timeline(
    item_id: str,
    variant_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Discard the user timeline on one of this item's variants + re-render from AI."""
    job = await _owned_item_render_job(item_id, user.id, db)
    await dispatch_reset_timeline(job, variant_id, db=db)
    log.info("plan_item_reset_timeline", item_id=item_id, variant_id=variant_id)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


# ── Reroll (swap idea for a single un-started item) ────────────────────────────


@router.post("/{item_id}/reroll", response_model=PlanItemResponse)
async def reroll_plan_item_route(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Re-generate the idea for a single plan item.

    Only allowed when the item is an un-started idea (item_status == "idea"
    and no current_job_id) — re-rolling a rendered/rendering item would
    orphan work in progress.
    """
    item = await _load_owned_item(item_id, user.id, db)

    if item.item_status != "idea" or item.current_job_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Can only reroll an un-started idea (item_status='idea', no current_job_id)",
        )

    item.item_status = "rerolling"
    await db.commit()

    from app.tasks.content_plan_build import reroll_plan_item  # noqa: PLC0415

    reroll_plan_item.delay(str(item.id))

    log.info("plan_item_reroll.dispatched", item_id=item_id)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))
