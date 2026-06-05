"""Persona endpoints (content-plan Phase 3). All require a real user.

POST  /personas        — submit the onboarding questionnaire, create the persona
                         row (status 'generating'), enqueue generation.
GET   /personas        — the current user's persona (1:1).
PATCH /personas/{id}   — hand-edit persona fields. Also the escape hatch when
                         generation fails: editing flips status to 'edited' and
                         unblocks onboarding regardless of the agent outcome.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
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


class PersonaResponse(BaseModel):
    id: str
    persona_status: str
    questionnaire: dict | None
    persona: dict | None
    error_detail: str | None

    @classmethod
    def of(cls, row: PersonaRow) -> PersonaResponse:
        return cls(
            id=str(row.id),
            persona_status=row.persona_status,
            questionnaire=row.questionnaire,
            persona=row.persona,
            error_detail=row.error_detail,
        )


# ── Routes ───────────────────────────────────────────────────────────────────


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
        existing.error_detail = None
        row = existing
    else:
        row = PersonaRow(
            user_id=user.id,
            questionnaire=questionnaire,
            persona_status="generating",
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
    await db.commit()

    from app.tasks.persona_build import retune_persona_from_feedback as retune_task  # noqa: PLC0415

    retune_task.delay(str(row.id))
    await db.refresh(row)
    return PersonaResponse.of(row)
