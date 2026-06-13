"""Persona endpoints (content-plan Phase 3). All require a real user.

POST  /personas        — submit the onboarding questionnaire, create the persona
                         row (status 'generating'), enqueue generation.
GET   /personas        — the current user's persona (1:1).
PATCH /personas/{id}   — hand-edit persona fields. Also the escape hatch when
                         generation fails: editing flips status to 'edited' and
                         unblocks onboarding regardless of the agent outcome.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.music_matcher import _sanitize_text
from app.auth import CurrentUser
from app.database import get_db
from app.models import Persona as PersonaRow
from app.models import User

log = structlog.get_logger()
router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────


class QuestionnaireBody(BaseModel):
    work: str = ""
    school: str = ""
    social: str = ""
    location: str = ""
    hobbies: str = ""
    travels: str = ""
    passions: str = ""
    tiktok_handle: str = ""


class IdeaSeed(BaseModel):
    """One user-owned content idea seed.

    id is server-stamped (uuid4 hex) on creation so PlanItem.source_idea_seed_id
    can reference it without a FK. Status defaults to "pending"; only T5 (provenance
    population) flips it to "in_plan".
    """

    id: str = ""  # filled by the server if empty/absent
    text: str
    pillar: str | None = None
    status: str = "pending"  # "pending" | "in_plan"


class PersonaEdit(BaseModel):
    """Partial edit — only provided fields are written."""

    summary: str | None = None
    content_pillars: list[str] | None = None
    tone: str | None = None
    audience: str | None = None
    posting_cadence: str | None = None
    # Structured post frequency: drives the number of plan ideas per week.
    # Integer so the merge loop stores it verbatim (never stringified). 422 on
    # out-of-range (ge/le). Omit to leave the existing value unchanged.
    posts_per_week: int | None = Field(default=None, ge=1, le=7)
    sample_topics: list[str] | None = None
    # Bring-Your-Own-Ideas (M1): user intent seeds persisted at persona scope.
    # When provided, replaces the full list (wholesale, same semantics as
    # content_pillars). Omit to leave the existing seeds unchanged. Seeds with
    # no id get server-stamped; text + pillar are sanitized before storage.
    idea_seeds: list[IdeaSeed] | None = None


class PersonaResponse(BaseModel):
    id: str
    persona_status: str
    questionnaire: dict | None
    persona: dict | None
    error_detail: str | None
    tiktok_profile: dict | None = None
    generation_started_at: datetime | None = None
    idea_seeds: list[dict] = []

    @classmethod
    def of(cls, row: PersonaRow) -> PersonaResponse:
        return cls(
            id=str(row.id),
            persona_status=row.persona_status,
            questionnaire=row.questionnaire,
            persona=row.persona,
            error_detail=row.error_detail,
            tiktok_profile=row.tiktok_profile,
            generation_started_at=row.generation_started_at,
            idea_seeds=list(row.idea_seeds or []),
        )


# ── Routes ───────────────────────────────────────────────────────────────────


class TikTokScrapeBody(BaseModel):
    handle: str


@router.post("/tiktok-scrape", response_model=PersonaResponse, status_code=status.HTTP_202_ACCEPTED)
async def tiktok_scrape(
    body: TikTokScrapeBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    """Pre-screen: accept a TikTok handle, fire async scrape, return persona row.

    Creates the persona row (status 'chat_pending') if one doesn't exist yet.
    The scrape runs in the background; the frontend polls GET /personas until
    tiktok_profile is populated (or a 10s timeout, whichever comes first).
    """
    from app.services.tiktok_profile import normalize_handle  # noqa: PLC0415
    from app.tasks.persona_build import scrape_tiktok_profile  # noqa: PLC0415

    clean_handle = normalize_handle(body.handle)

    existing = (
        await db.execute(select(PersonaRow).where(PersonaRow.user_id == user.id))
    ).scalar_one_or_none()

    if existing is None:
        row = PersonaRow(
            user_id=user.id,
            questionnaire={"tiktok_handle": clean_handle},
            persona_status="chat_pending",
        )
        db.add(row)
    else:
        q = dict(existing.questionnaire or {})
        q["tiktok_handle"] = clean_handle
        existing.questionnaire = q
        row = existing

    await db.commit()
    await db.refresh(row)

    scrape_tiktok_profile.delay(str(row.id), clean_handle)
    return PersonaResponse.of(row)


@router.post("", response_model=PersonaResponse, status_code=status.HTTP_201_CREATED)
async def create_persona(
    body: QuestionnaireBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    """Create-or-replace the user's persona and enqueue generation.

    1:1 with the user — re-submitting the questionnaire resets the existing row
    rather than creating a second persona.
    """
    existing = (
        await db.execute(select(PersonaRow).where(PersonaRow.user_id == user.id))
    ).scalar_one_or_none()

    questionnaire = body.model_dump()
    if existing is not None:
        existing.questionnaire = questionnaire
        existing.persona = None
        existing.persona_status = "generating"
        existing.generation_started_at = datetime.now(UTC)
        existing.error_detail = None
        row = existing
    else:
        row = PersonaRow(
            user_id=user.id,
            questionnaire=questionnaire,
            persona_status="generating",
            generation_started_at=datetime.now(UTC),
        )
        db.add(row)
    await db.commit()
    await db.refresh(row)

    # Enqueue off-Job generation. Import lazily so the API process doesn't pull
    # the agent/model client at module load.
    from app.tasks.persona_build import generate_persona  # noqa: PLC0415

    generate_persona.delay(str(row.id))
    return PersonaResponse.of(row)


@router.get("", response_model=PersonaResponse)
async def get_persona(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    row = (
        await db.execute(select(PersonaRow).where(PersonaRow.user_id == user.id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No persona yet")
    return PersonaResponse.of(row)


class ResetResponse(BaseModel):
    reset: bool


@router.post("/reset", response_model=ResetResponse)
async def reset_persona(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ResetResponse:
    """Soft-reset the user's onboarding profile so they can redo it from scratch.

    Deletes the persona row (cascades content_plans → plan_items + all chat/style
    state stored on the persona JSONB), explicitly deletes video_feedback, and
    resets onboarding_status back to 'pending'. Rendered videos (jobs) are KEPT.

    FK hazard: Job.content_plan_item_id → plan_items.id has no ondelete. We NULL
    those refs first (models.py:383) so the plan_items cascade doesn't raise a
    Postgres FK violation. Celery tasks (generate_persona, derive_user_style, etc.)
    all no-op on a missing persona row — no recreate path. Idempotent: returns 200
    even when the user has no persona yet.
    """
    from app.models import Job, VideoFeedback  # noqa: PLC0415

    # 1. Sever job → plan_item back-refs BEFORE plan_items are cascade-deleted.
    #    Without this, the persona delete cascades into plan_items while jobs still
    #    hold a no-ondelete FK → Postgres NO ACTION violation.
    await db.execute(
        update(Job)
        .where(Job.user_id == user.id, Job.content_plan_item_id.is_not(None))
        .values(content_plan_item_id=None)
    )
    # 2. Delete user-scoped feedback (ondelete=CASCADE is from users, not here).
    await db.execute(delete(VideoFeedback).where(VideoFeedback.user_id == user.id))
    # 3. Delete the persona via raw SQL — NOT db.delete(row). Using the ORM
    #    db.delete() triggers SQLAlchemy's cascade handling which tries to SET
    #    persona_id=NULL on content_plans before the row is deleted, violating
    #    the NOT NULL constraint. A raw DELETE lets Postgres's ondelete=CASCADE
    #    handle child rows directly at the DB level.
    result = await db.execute(delete(PersonaRow).where(PersonaRow.user_id == user.id))
    had_persona = result.rowcount > 0
    # 4. Reset onboarding so the frontend routes back to setup:prescreen.
    db_user = await db.get(User, user.id)
    if db_user is not None:
        db_user.onboarding_status = "pending"
    await db.commit()
    log.info("reset_persona", user_id=str(user.id), had_persona=had_persona)
    return ResetResponse(reset=True)


@router.patch("/{persona_id}", response_model=PersonaResponse)
async def edit_persona(
    persona_id: str,
    edit: PersonaEdit,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    try:
        pid = uuid.UUID(persona_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc

    row = await db.get(PersonaRow, pid)
    if row is None or row.user_id != user.id:
        # Don't leak existence of other users' personas.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Persona not found")

    # Merge edits onto the current persona dict, sanitizing all user-supplied
    # text (it later threads into downstream agent prompts).
    persona = dict(row.persona or {})
    updates = edit.model_dump(exclude_none=True)

    # idea_seeds lives on the top-level row column, not inside the persona JSONB —
    # handle it before the main merge loop and pop it so it doesn't bleed in.
    if "idea_seeds" in updates:
        raw_seeds: list[dict] = updates.pop("idea_seeds")
        _VALID_STATUSES = {"pending", "in_plan"}
        stamped: list[dict] = []
        for s in raw_seeds:
            seed_id = str(s.get("id") or "").strip() or uuid.uuid4().hex
            seed_text = _sanitize_text(str(s.get("text") or ""))
            if not seed_text:
                continue  # drop blank seeds
            seed_pillar_raw = s.get("pillar")
            seed_pillar = _sanitize_text(str(seed_pillar_raw)) if seed_pillar_raw else None
            seed_status = s.get("status", "pending")
            if seed_status not in _VALID_STATUSES:
                seed_status = "pending"
            stamped.append(
                {"id": seed_id, "text": seed_text, "pillar": seed_pillar, "status": seed_status}
            )
        row.idea_seeds = stamped

    for key, value in updates.items():
        if isinstance(value, list):
            persona[key] = [v for v in (_sanitize_text(str(x)) for x in value) if v]
        elif isinstance(value, int) and not isinstance(value, bool):
            # Store integers verbatim — running _sanitize_text(str(4)) → "4" would
            # work but loses the type; Persona(**persona) would then accept "4" as
            # a str where int is expected. Booleans are excluded (isinstance(True, int)
            # is True in Python) — they're not in PersonaEdit but guards future fields.
            persona[key] = value
        else:
            persona[key] = _sanitize_text(str(value))

    row.persona = persona
    row.persona_status = "edited"
    row.error_detail = None
    # A hand-edit always unblocks onboarding, even after a failed generation.
    db_user = await db.get(User, user.id)
    if db_user is not None and db_user.onboarding_status == "pending":
        db_user.onboarding_status = "persona_ready"
    await db.commit()
    await db.refresh(row)
    return PersonaResponse.of(row)


@router.post("/{persona_id}/retune-from-feedback", response_model=PersonaResponse)
async def retune_persona_from_feedback(
    persona_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    """Re-run persona generation with the user's feedback as context (Phase 2).

    The "their say" invariant: a hand-edited persona is authoritative and is NEVER
    overwritten by inferred feedback — so we 409 when status is 'edited' (the user
    must reset to AI or edit directly). For an AI-authored persona, this re-tunes
    the lane toward what they reacted well to. Best-effort: the task leaves the
    existing persona untouched on failure.
    """
    try:
        pid = uuid.UUID(persona_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc

    row = await db.get(PersonaRow, pid)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Persona not found")
    if row.persona_status == "edited":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Your persona is hand-edited and stays authoritative. "
            "Edit it directly, or reset to AI before retuning from feedback.",
        )
    if row.persona is None or row.persona_status == "generating":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Persona must be ready before retuning",
        )

    row.persona_status = "generating"
    row.generation_started_at = datetime.now(UTC)
    await db.commit()

    from app.tasks.persona_build import retune_persona_from_feedback as retune_task  # noqa: PLC0415

    retune_task.delay(str(row.id))
    await db.refresh(row)
    return PersonaResponse.of(row)


# ── Style routes (Creator Agent M1) ──────────────────────────────────────────


class StyleKnobsEdit(BaseModel):
    """Partial knob edit — any subset of StyleKnobs fields. Validated at write time."""

    font_family: str | None = None
    text_size_px: int | None = Field(default=None, ge=40, le=80)
    position: str | None = None
    position_x_frac: float | None = Field(default=None, ge=0.0, le=1.0)
    position_y_frac: float | None = Field(default=None, ge=0.0, le=1.0)
    text_anchor: str | None = None
    text_color: str | None = None
    highlight_color: str | None = None
    stroke_width: int | None = None
    cycle_fonts: bool | None = None


class StyleEdit(BaseModel):
    """PATCH /personas/style — any subset of UserStyle top-level fields."""

    style_set_id: str | None = None
    knobs: StyleKnobsEdit | None = None
    footage_type_bias: list[str] | None = None
    preferred_edit_format_mix: dict | None = None
    instruction_level: str | None = None  # full | light | none


class StyleResponse(BaseModel):
    """GET /personas/style response — wraps the stored UserStyle dict."""

    style: dict | None
    status: str  # deriving | ready | edited | failed | absent
    style_set_preview: dict | None = None  # label, tags, preview_url from catalog
    font_preview: dict | None = None  # css_family, display_name for the UI typeface


def _style_set_preview(style_set_id: str | None) -> dict | None:
    """Return the style-set preview dict (font, colors, effect) for the UI picker."""
    if not style_set_id:
        return None
    try:
        from app.pipeline.style_sets import style_set_preview  # noqa: PLC0415

        return {"id": style_set_id, **style_set_preview(style_set_id)}
    except Exception:  # noqa: BLE001
        return None


def _font_preview(font_family: str | None) -> dict | None:
    """Return display metadata for the pinned font (css_family for the UI)."""
    if not font_family:
        return None
    try:
        from app.pipeline.text_overlay import _FONT_REGISTRY  # noqa: PLC0415

        entry = _FONT_REGISTRY.get(font_family) or {}
        return {
            "font_family": font_family,
            "display_name": entry.get("display_name") or font_family,
            "css_family": entry.get("css_family") or font_family,
        }
    except Exception:  # noqa: BLE001
        return None


@router.get("/style", response_model=StyleResponse)
async def get_style(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> StyleResponse:
    """Return the current user's derived style (or absent when not yet derived)."""
    from app.config import settings  # noqa: PLC0415

    if not settings.user_style_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="style_not_enabled")
    result = await db.execute(select(PersonaRow).where(PersonaRow.user_id == user.id))
    row = result.scalar_one_or_none()
    if row is None:
        return StyleResponse(style=None, status="absent")
    raw = dict(row.style) if row.style else None
    if raw is None:
        # Persona exists but no style — write "deriving" first (prevents re-queue
        # on concurrent requests), then kick off derivation in the background.
        row.style = {"status": "deriving"}
        await db.commit()
        try:
            from app.tasks.style_build import derive_user_style  # noqa: PLC0415

            derive_user_style.delay(str(row.id))
        except Exception:  # noqa: BLE001
            pass
        return StyleResponse(style=None, status="deriving")
    pinned_set_id = raw.get("style_set_id")
    pinned_font = (raw.get("knobs") or {}).get("font_family")
    return StyleResponse(
        style=raw,
        status=raw.get("status", "ready"),
        style_set_preview=_style_set_preview(pinned_set_id),
        font_preview=_font_preview(pinned_font),
    )


async def _apply_style_edit(row: PersonaRow, edit: StyleEdit, db: AsyncSession) -> dict:
    """Read-merge-write the style edit onto the persona row and commit.

    Shared by PATCH /personas/style and POST /personas/agent/turn so the two
    write paths can never drift. Returns the final merged style dict.
    Raises HTTPException on validation errors (unknown set_id / font_family /
    instruction_level).
    """
    raw: dict = dict(row.style) if row.style else {}

    # Validate style_set_id against catalog when provided.
    if edit.style_set_id is not None:
        try:
            from app.pipeline.style_sets import style_set_ids  # noqa: PLC0415

            known = style_set_ids()
            if edit.style_set_id not in known:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unknown style_set_id: {edit.style_set_id}",
                )
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            pass  # catalog unavailable → accept; fail-open on preview
        raw["style_set_id"] = edit.style_set_id

    # Validate + merge knobs (only provided fields overwrite).
    if edit.knobs is not None:
        knobs = dict(raw.get("knobs") or {})
        knob_data = edit.knobs.model_dump(exclude_none=True)
        # Validate font_family against registry if provided.
        if "font_family" in knob_data:
            try:
                from app.pipeline.text_overlay import _FONT_REGISTRY  # noqa: PLC0415

                if knob_data["font_family"] not in _FONT_REGISTRY:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Unknown font_family: {knob_data['font_family']}",
                    )
            except HTTPException:
                raise
            except Exception:  # noqa: BLE001
                pass  # registry unavailable → accept; fail-open
        knobs.update(knob_data)
        raw["knobs"] = knobs

    if edit.footage_type_bias is not None:
        raw["footage_type_bias"] = edit.footage_type_bias
    if edit.preferred_edit_format_mix is not None:
        raw["preferred_edit_format_mix"] = edit.preferred_edit_format_mix
    if edit.instruction_level is not None:
        allowed = {"full", "light", "none"}
        if edit.instruction_level not in allowed:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"instruction_level must be one of {sorted(allowed)}",
            )
        raw["instruction_level"] = edit.instruction_level

    # Mark as edited — derivation guards will not auto-overwrite.
    raw["status"] = "edited"
    row.style = raw
    await db.commit()
    await db.refresh(row)
    return dict(row.style)


@router.patch("/style", response_model=StyleResponse)
async def patch_style(
    edit: StyleEdit,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> StyleResponse:
    """Partial-edit the user's style. Sets status='edited' — derivation will not auto-overwrite."""
    from app.config import settings  # noqa: PLC0415

    if not settings.user_style_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="style_not_enabled")

    result = await db.execute(select(PersonaRow).where(PersonaRow.user_id == user.id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="persona_not_found")

    merged = await _apply_style_edit(row, edit, db)
    pinned_set_id = merged.get("style_set_id")
    pinned_font = (merged.get("knobs") or {}).get("font_family")
    return StyleResponse(
        style=merged,
        status="edited",
        style_set_preview=_style_set_preview(pinned_set_id),
        font_preview=_font_preview(pinned_font),
    )


@router.post("/style/rederive", status_code=status.HTTP_202_ACCEPTED)
async def rederive_style(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Re-derive the style from the current persona (overwrites even an edited style)."""
    from app.config import settings  # noqa: PLC0415

    if not settings.user_style_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="style_not_enabled")

    result = await db.execute(select(PersonaRow).where(PersonaRow.user_id == user.id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="persona_not_found")
    if row.persona_status not in ("ready", "edited"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="persona_not_ready",
        )

    # Mark as deriving so the UI can poll status.
    raw: dict = dict(row.style) if row.style else {}
    raw["status"] = "deriving"
    row.style = raw
    await db.commit()

    from app.tasks.style_build import derive_user_style  # noqa: PLC0415

    # force=True bypasses the "edited" guard in the task.
    derive_user_style.delay(str(row.id), force=True)
    return {"queued": True, "persona_id": str(row.id)}


# ── Chat interview routes ─────────────────────────────────────────────────────


def _tiktok_summary(profile: dict) -> str:
    """Format a scraped TikTok profile dict into a text summary for the interviewer."""
    parts: list[str] = []
    if profile.get("handle"):
        parts.append(f"@{profile['handle']}")
    if profile.get("follower_count"):
        c = profile["follower_count"]
        parts.append(f"{c / 1000:.1f}K followers" if c >= 1000 else f"{c} followers")
    if profile.get("video_count"):
        parts.append(f"{profile['video_count']} videos")
    header = " · ".join(parts) if parts else "(profile loaded)"
    lines = [header]
    if profile.get("top_hashtags"):
        lines.append("Top hashtags: " + ", ".join(profile["top_hashtags"][:5]))
    if profile.get("top_captions"):
        lines.append("Sample captions: " + "; ".join(profile["top_captions"][:3]))
    return "\n".join(lines)


class ChatStartResponse(BaseModel):
    persona_id: str
    question: str
    suggestions: list[str] = []
    turn_number: int
    turn_label: str
    tiktok_context: dict | None = None
    persona_status: str


class ChatTurnBody(BaseModel):
    persona_id: str
    answer: str


class ChatTurnResponse(BaseModel):
    question: str | None = None
    suggestions: list[str] = []
    is_final: bool
    turn_number: int
    turn_label: str
    persona_status: str


@router.post("/chat/start", response_model=ChatStartResponse)
async def chat_start(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ChatStartResponse:
    """Start (or resume) the onboarding chat interview.

    Gets or creates the Persona row (status 'chat_pending'). If the user
    has already begun and the last unanswered turn is an agent question,
    resumes from there (no extra LLM call). Otherwise calls InterviewerAgent
    for the first question. Suggestions + turn_label are stored in the turn
    dict so resume is free.
    """
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents.interviewer_agent import (  # noqa: PLC0415
        ConversationTurn,
        InterviewerAgent,
        InterviewerInput,
    )

    existing = (
        await db.execute(select(PersonaRow).where(PersonaRow.user_id == user.id))
    ).scalar_one_or_none()

    if existing is None:
        row = PersonaRow(
            user_id=user.id,
            questionnaire={"interview_turns": []},
            persona_status="chat_pending",
        )
        db.add(row)
        try:
            await db.commit()
            await db.refresh(row)
        except IntegrityError:
            # tiktok_scrape beat us to the INSERT — re-fetch the row it created.
            await db.rollback()
            row = (
                await db.execute(select(PersonaRow).where(PersonaRow.user_id == user.id))
            ).scalar_one()
    else:
        await db.refresh(existing)
        row = existing

    # If already past the chat stage, surface the status so the frontend can redirect
    # without crashing (empty question; persona_status drives the routing decision).
    if row.persona_status in ("generating", "ready", "edited"):
        return ChatStartResponse(
            persona_id=str(row.id),
            question="",
            persona_status=row.persona_status,
            turn_number=0,
            turn_label="",
        )

    q = copy.deepcopy(row.questionnaire or {})
    turns_raw: list[dict] = q.get("interview_turns", [])

    # Resume: last turn is an unanswered agent question — return it without re-calling.
    if turns_raw and turns_raw[-1].get("role") == "agent":
        last_agent = turns_raw[-1]
        agent_count = sum(1 for t in turns_raw if t.get("role") == "agent")
        return ChatStartResponse(
            persona_id=str(row.id),
            question=last_agent["content"],
            suggestions=last_agent.get("suggestions", []),
            turn_number=agent_count,
            turn_label=last_agent.get("turn_label") or f"~{agent_count} OF ~6",
            tiktok_context=row.tiktok_profile,
            persona_status=row.persona_status,
        )

    # Fresh start or crash-recovery (last turn was user) — call agent for next Q.
    tiktok_summary = _tiktok_summary(row.tiktok_profile) if row.tiktok_profile else None
    conv_turns = [ConversationTurn(role=t["role"], content=t["content"]) for t in turns_raw]
    agent_count = sum(1 for t in turns_raw if t.get("role") == "agent")

    result = await asyncio.to_thread(
        InterviewerAgent(default_client()).run,
        InterviewerInput(
            turns=conv_turns, tiktok_summary=tiktok_summary, turn_count=agent_count + 1
        ),
    )

    # turn_label comes from InterviewerAgent.parse(), which derives N from the
    # same counter we pass as turn_count and clamps the total (N ≤ M ≤ 7,
    # final → M = N). The old hardcoded "OF ~6" here produced "~7 OF ~6" the
    # moment the interview ran long (dogfood 2026-06-12).
    computed_label = result.turn_label

    # Store Q + metadata so resume is free (no re-call on page refresh).
    turns_raw.append(
        {
            "role": "agent",
            "content": result.question,
            "suggestions": result.suggestions,
            "turn_label": computed_label,
        }
    )
    q["interview_turns"] = turns_raw
    row.questionnaire = q
    await db.commit()

    return ChatStartResponse(
        persona_id=str(row.id),
        question=result.question,
        suggestions=result.suggestions,
        turn_number=agent_count + 1,
        turn_label=computed_label,
        tiktok_context=row.tiktok_profile,
        persona_status=row.persona_status,
    )


@router.post("/chat/turn", response_model=ChatTurnResponse)
async def chat_turn(
    body: ChatTurnBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ChatTurnResponse:
    """Submit a chat answer and get the next question (or finalize).

    Appends the user answer to interview_turns, calls InterviewerAgent for the
    next Q. If the agent signals is_final (or the hard cap of 8 agent turns is
    reached), fires generate_persona.delay() and returns is_final=True.
    """
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents.interviewer_agent import (  # noqa: PLC0415
        _HARD_CAP,
        ConversationTurn,
        InterviewerAgent,
        InterviewerInput,
    )

    try:
        pid = uuid.UUID(body.persona_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="bad persona_id"
        ) from exc

    row = await db.get(PersonaRow, pid)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Persona not found")
    if row.persona_status not in ("chat_pending", "failed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Chat not active (status: {row.persona_status})",
        )

    q = copy.deepcopy(row.questionnaire or {})
    turns_raw: list[dict] = q.get("interview_turns", [])

    # If the previous stored agent turn was already marked final, this answer
    # is the last one — generate without asking the agent again.
    last_agent_was_final = (
        bool(turns_raw)
        and turns_raw[-1].get("role") == "agent"
        and turns_raw[-1].get("is_final", False)
    )

    turns_raw.append({"role": "user", "content": _sanitize_text(body.answer)})
    agent_count = sum(1 for t in turns_raw if t.get("role") == "agent")

    from app.tasks.persona_build import generate_persona  # noqa: PLC0415

    # Finalize: hard cap hit, or user just answered the stored final question.
    if agent_count >= _HARD_CAP or last_agent_was_final:
        q["interview_turns"] = turns_raw
        row.questionnaire = q
        row.persona_status = "generating"
        row.generation_started_at = datetime.now(UTC)
        await db.commit()
        generate_persona.delay(str(row.id))
        return ChatTurnResponse(
            is_final=True,
            turn_number=agent_count,
            turn_label=f"~{agent_count} OF ~{_HARD_CAP}",
            persona_status="generating",
        )

    tiktok_summary = _tiktok_summary(row.tiktok_profile) if row.tiktok_profile else None
    conv_turns = [ConversationTurn(role=t["role"], content=t["content"]) for t in turns_raw]

    result = await asyncio.to_thread(
        InterviewerAgent(default_client()).run,
        InterviewerInput(
            turns=conv_turns, tiktok_summary=tiktok_summary, turn_count=agent_count + 1
        ),
    )

    new_agent_count = agent_count + 1
    agent_is_final = result.is_final or new_agent_count >= _HARD_CAP

    # turn_label comes from InterviewerAgent.parse() — same contract as
    # chat/start (N from our counter, N ≤ M ≤ 7, final → M = N).
    computed_label = result.turn_label

    # Store the question; if agent flagged it as final, mark it so the NEXT
    # answer triggers generation (deferred — user must answer this Q first).
    turns_raw.append(
        {
            "role": "agent",
            "content": result.question,
            "suggestions": result.suggestions,
            "turn_label": computed_label,
            "is_final": agent_is_final,
        }
    )
    q["interview_turns"] = turns_raw
    row.questionnaire = q
    await db.commit()

    # Always return is_final=False so the frontend shows the question and waits
    # for the user's answer before transitioning to the generating state.
    return ChatTurnResponse(
        question=result.question,
        suggestions=result.suggestions,
        is_final=False,
        turn_number=new_agent_count,
        turn_label=computed_label,
        persona_status="chat_pending",
    )


# ── Style Agent routes (Creator Agent M2) ──────────────────────────────────────


class StyleAgentTurnBody(BaseModel):
    answer: str
    prior_turns: list[dict] = Field(default_factory=list)


class StyleAgentTurnResponse(BaseModel):
    reply: str
    suggestions: list[str] = Field(default_factory=list)
    applied: bool
    intent: str
    persona_status: str


def _style_snapshot(row: PersonaRow) -> dict | None:
    """Full snapshot of the user's current style for the agent.

    Exposes all 10 knobs + top-level fields so the agent can answer
    read-back queries ("what is it set to right now?") accurately.
    """
    if not row.style:
        return None
    raw = dict(row.style)
    return {
        "style_set_id": raw.get("style_set_id"),
        "instruction_level": raw.get("instruction_level"),
        "footage_type_bias": raw.get("footage_type_bias", []),
        "preferred_edit_format_mix": raw.get("preferred_edit_format_mix", {}),
        "status": raw.get("status"),
        # All 10 parity-safe knobs (only non-None values).
        "knobs": {k: v for k, v in (raw.get("knobs") or {}).items() if v is not None},
    }


@router.post("/agent/start", response_model=StyleAgentTurnResponse)
async def style_agent_start(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> StyleAgentTurnResponse:
    """Return a personalised greeting + opening suggestion chips for the style agent.

    Gated behind `settings.style_agent_enabled` (404 when off).
    """
    from app.config import settings  # noqa: PLC0415

    if not settings.style_agent_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="style_agent_not_enabled")

    result = await db.execute(select(PersonaRow).where(PersonaRow.user_id == user.id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="persona_not_found")

    snapshot = _style_snapshot(row)
    if snapshot and snapshot.get("style_set_id"):
        greeting = (
            f'Your style is set to "{snapshot["style_set_id"]}". '
            "Tell me what you'd like to change — your font, text size, filming focus, "
            "or how much detail you want in your content plans."
        )
    else:
        greeting = (
            "Tell me how you'd like your videos to look and feel. "
            'You can say things like "make my font bigger", "I mostly film outdoors", '
            'or "keep my plans minimal".'
        )

    return StyleAgentTurnResponse(
        reply=greeting,
        suggestions=[
            "Make my font bigger",
            "I mostly film outdoors",
            "Keep my plans minimal",
            "Change my text style",
        ],
        applied=False,
        intent="greeting",
        persona_status=row.persona_status,
    )


@router.post("/agent/turn", response_model=StyleAgentTurnResponse)
async def style_agent_turn(
    body: StyleAgentTurnBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> StyleAgentTurnResponse:
    """Process one style-agent conversational turn.

    Parses the user utterance into a typed intent, routes to the correct write path,
    and returns a reply + suggestion chips.
    Gated behind `settings.style_agent_enabled` (404 when off).
    """
    from app.config import settings  # noqa: PLC0415

    if not settings.style_agent_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="style_agent_not_enabled")

    result = await db.execute(select(PersonaRow).where(PersonaRow.user_id == user.id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="persona_not_found")

    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents.style_intent import StyleIntentAgent, StyleIntentInput  # noqa: PLC0415

    snapshot = _style_snapshot(row)
    agent_input = StyleIntentInput(
        utterance=body.answer,
        prior_turns=body.prior_turns,
        current_style_snapshot=snapshot,
    )

    try:
        intent_result = await asyncio.to_thread(
            StyleIntentAgent(default_client()).run,
            agent_input,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("style_agent_turn.agent_failed", error=str(exc), user_id=str(user.id))
        # Store the failed turn for diagnostics, then surface a friendly fallback.
        q = copy.deepcopy(row.questionnaire or {})
        turns: list[dict] = q.get("style_agent_turns", [])
        turns.append({"role": "user", "content": body.answer, "error": str(exc)})
        q["style_agent_turns"] = turns
        row.questionnaire = q
        await db.commit()
        _fallback_reply = (
            "I had trouble understanding that. Try rephrasing — "
            'for example, "make my font bigger" or "I film mostly outdoors".'
        )
        return StyleAgentTurnResponse(
            reply=_fallback_reply,
            suggestions=["Make my font bigger", "I mostly film outdoors", "Keep plans minimal"],
            applied=False,
            intent="unknown",
            persona_status=row.persona_status,
        )

    # Store the turn in questionnaire["style_agent_turns"] (disjoint from interview_turns).
    q = copy.deepcopy(row.questionnaire or {})
    turns = q.get("style_agent_turns", [])
    turns.append(
        {
            "role": "user",
            "content": body.answer,
            "intent": intent_result.intent,
            "confidence": intent_result.confidence,
        }
    )
    turns.append(
        {
            "role": "agent",
            "content": intent_result.reply,
            "intent": intent_result.intent,
            "applied": False,  # updated below if write succeeds
        }
    )
    q["style_agent_turns"] = turns
    row.questionnaire = q
    # Persist the turn log before attempting writes so we never lose it.
    await db.commit()

    applied = False
    # May be updated inside the style_edit branch to append honest clamp notes.
    final_reply = intent_result.reply

    # Route to the correct write path based on intent.
    if intent_result.needs_clarification or intent_result.intent == "clarify":
        # No write — return clarifying question.
        pass

    elif intent_result.intent in ("style_edit", "scope_reduction"):
        # Build a StyleEdit from the agent fields — only parity-safe knobs allowed.
        # IMPORTANT: build StyleKnobsEdit from the CLAMPED output of StyleKnobs, never
        # from raw_knobs directly. StyleKnobs._clamp_px silently clamps text_size_px to
        # [40, 80]; StyleKnobsEdit has ge=40 which raises on raw sub-floor values. Using
        # clamped values keeps the two validators in sync and prevents a 500.
        fields = intent_result.fields or {}

        raw_knobs = fields.get("knobs") or {}
        knobs_edit = None
        _clamp_notes: list[str] = []

        if raw_knobs:
            from pydantic import ValidationError  # noqa: PLC0415

            from app.agents._schemas.user_style import StyleKnobs  # noqa: PLC0415

            try:
                validated_knobs = StyleKnobs.model_validate(raw_knobs)
            except ValidationError as exc:
                # extra="forbid" caught a key that is not in the 10 parity-safe knobs.
                log.warning(
                    "style_agent_turn.forbidden_knob",
                    knobs=raw_knobs,
                    error=str(exc),
                    user_id=str(user.id),
                )
                _knob_reply = (
                    "I can only adjust font, size, position, color, or stroke settings. "
                    "Could you be more specific about what to change?"
                )
                return StyleAgentTurnResponse(
                    reply=_knob_reply,
                    suggestions=["Change my font", "Make text bigger", "Move text to center"],
                    applied=False,
                    intent=intent_result.intent,
                    persona_status=row.persona_status,
                )

            # Build from the clamped dict — StyleKnobsEdit never raises on these values.
            clamped = validated_knobs.model_dump(exclude_none=True)

            # Detect text_size_px clamping so the reply is honest about what was stored.
            raw_px = raw_knobs.get("text_size_px")
            clamped_px = clamped.get("text_size_px")
            if raw_px is not None and clamped_px is not None:
                try:
                    if int(raw_px) != int(clamped_px):
                        _clamp_notes.append(
                            f"(Smallest text size is 40px — I've set it to {clamped_px}.)"
                        )
                except (TypeError, ValueError):
                    pass

            if clamped:
                knobs_edit = StyleKnobsEdit(**clamped)

        # Materiality check: skip the write if there is nothing concrete to persist.
        # A free_text-only style_edit has no structured knob/field; calling _apply_style_edit
        # would stamp status="edited" without changing any setting (false "Done").
        is_material = bool(
            (knobs_edit is not None)
            or fields.get("style_set_id")
            or fields.get("footage_type_bias")
            or fields.get("preferred_edit_format_mix")
            or fields.get("instruction_level")
        )
        if not is_material:
            return StyleAgentTurnResponse(
                reply=(
                    "To apply a change, try being specific — for example: "
                    '"make text 44px", "move text to bottom", or "use a serif font".'
                ),
                suggestions=["Make text 44px", "Move text to bottom", "Change my font"],
                applied=False,
                intent=intent_result.intent,
                persona_status=row.persona_status,
            )

        style_edit = StyleEdit(
            style_set_id=fields.get("style_set_id"),
            knobs=knobs_edit,
            footage_type_bias=fields.get("footage_type_bias"),
            preferred_edit_format_mix=fields.get("preferred_edit_format_mix"),
            instruction_level=fields.get("instruction_level"),
        )

        try:
            await _apply_style_edit(row, style_edit, db)
            applied = True
            # Append honest clamp note when text_size_px was adjusted.
            if _clamp_notes:
                final_reply = intent_result.reply + " " + " ".join(_clamp_notes)
            # Update the last agent turn to reflect the write.
            q2 = copy.deepcopy(row.questionnaire or {})
            turns2 = q2.get("style_agent_turns", [])
            if turns2:
                turns2[-1]["applied"] = True
            q2["style_agent_turns"] = turns2
            row.questionnaire = q2
            await db.commit()
        except HTTPException:
            # Validation error from _apply_style_edit (unknown set/font) — bubble reply.
            pass

    elif intent_result.intent == "persona_preference":
        # Soft preference → retune the persona (which will re-derive the style).
        if row.persona_status in ("ready", "edited"):
            from app.tasks.persona_build import retune_persona_from_feedback  # noqa: PLC0415

            retune_persona_from_feedback.delay(str(row.id))
            applied = True  # task is queued, not applied synchronously

    # intent == "describe" | "unknown" → no write, use reply from agent.

    return StyleAgentTurnResponse(
        reply=final_reply,
        suggestions=intent_result.suggestions,
        applied=applied,
        intent=intent_result.intent,
        persona_status=row.persona_status,
    )


# ── Onboarding fork ───────────────────────────────────────────────────────────


class OnboardingForkRequest(BaseModel):
    content_mode: str  # "existing_footage" | "create_new" | "mixed"
    topic: str | None = None  # what their footage is about
    intent: str | None = None  # what they want viewers to feel/do
    # Optional: durable clip paths from the onboarding edit job (server-derived,
    # lifecycle-exempt under generative-jobs/*/sources/). Stored in questionnaire
    # so create_plan can carry them as seed_clip_paths without the 422 prefix guard.
    onboarding_clip_paths: list[str] | None = None
    onboarding_edit_job_id: str | None = None


class OnboardingForkResponse(BaseModel):
    persona_id: str
    persona_status: str


@router.post("/onboarding-fork", response_model=OnboardingForkResponse)
async def onboarding_fork(
    body: OnboardingForkRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OnboardingForkResponse:
    """Record the onboarding fork choice (content_mode, topic, intent) on the persona.

    Gets or creates the Persona row using the same race-safe pattern as chat_start.
    Merges into `questionnaire` (read-modify-write) — NEVER clobbers existing keys
    so a post-scrape tiktok_handle is preserved.

    Seeds interview_turns with a synthetic user turn (answered) so chat_start opens
    at the NEXT question rather than re-asking what the footage is about.
    Does NOT leave an unanswered agent turn as the last entry — that would trigger
    the resume-without-LLM stall in chat_start (~line 668).
    """
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    existing = (
        await db.execute(select(PersonaRow).where(PersonaRow.user_id == user.id))
    ).scalar_one_or_none()

    if existing is None:
        row = PersonaRow(
            user_id=user.id,
            questionnaire={"interview_turns": []},
            persona_status="chat_pending",
        )
        db.add(row)
        try:
            await db.commit()
            await db.refresh(row)
        except IntegrityError:
            # tiktok_scrape or chat_start beat us to the INSERT — re-fetch.
            await db.rollback()
            row = (
                await db.execute(select(PersonaRow).where(PersonaRow.user_id == user.id))
            ).scalar_one()
    else:
        await db.refresh(existing)
        row = existing

    # Read-modify-write: preserve all existing questionnaire keys (tiktok_handle,
    # interview_turns from a prior scrape, etc.).
    q = dict(row.questionnaire or {})
    q["content_mode"] = body.content_mode
    if body.topic:
        q["onboarding_topic"] = body.topic
    if body.intent:
        q["onboarding_intent"] = body.intent
    if body.onboarding_clip_paths is not None:
        q["onboarding_clip_paths"] = body.onboarding_clip_paths
    if body.onboarding_edit_job_id is not None:
        q["onboarding_edit_job_id"] = body.onboarding_edit_job_id

    # Seed a synthetic answered-user turn so chat_start asks the NEXT question.
    # Only seed if no prior interview_turns exist (respect any previous chat progress).
    # Never leave an agent turn as the last entry — that stalls the resume branch.
    if body.topic or body.intent:
        existing_turns = q.get("interview_turns", [])
        if not existing_turns:
            seed_text = body.topic or ""
            if body.intent:
                seed_text = f"{seed_text}. I want viewers to {body.intent}".strip(". ")
            q["interview_turns"] = [{"role": "user", "content": seed_text}]

    row.questionnaire = q
    await db.commit()

    return OnboardingForkResponse(
        persona_id=str(row.id),
        persona_status=row.persona_status,
    )
