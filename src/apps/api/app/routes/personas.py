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
    tiktok_profile: dict | None = None

    @classmethod
    def of(cls, row: PersonaRow) -> PersonaResponse:
        return cls(
            id=str(row.id),
            persona_status=row.persona_status,
            questionnaire=row.questionnaire,
            persona=row.persona,
            error_detail=row.error_detail,
            tiktok_profile=row.tiktok_profile,
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
        await db.commit()
        await db.refresh(row)
    else:
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

    q = dict(row.questionnaire or {})
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
            turn_label=last_agent.get("turn_label", f"~{agent_count} OF ~6"),
            tiktok_context=row.tiktok_profile,
            persona_status=row.persona_status,
        )

    # Fresh start or crash-recovery (last turn was user) — call agent for next Q.
    tiktok_summary = _tiktok_summary(row.tiktok_profile) if row.tiktok_profile else None
    conv_turns = [ConversationTurn(role=t["role"], content=t["content"]) for t in turns_raw]
    agent_count = sum(1 for t in turns_raw if t.get("role") == "agent")

    result = await asyncio.to_thread(
        InterviewerAgent().run,
        InterviewerInput(turns=conv_turns, tiktok_summary=tiktok_summary, turn_count=agent_count),
    )

    # Store Q + metadata so resume is free (no re-call on page refresh).
    turns_raw.append({
        "role": "agent",
        "content": result.question,
        "suggestions": result.suggestions,
        "turn_label": result.turn_label,
    })
    q["interview_turns"] = turns_raw
    row.questionnaire = q
    await db.commit()

    return ChatStartResponse(
        persona_id=str(row.id),
        question=result.question,
        suggestions=result.suggestions,
        turn_number=agent_count + 1,
        turn_label=result.turn_label or f"~{agent_count + 1} OF ~6",
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

    q = dict(row.questionnaire or {})
    turns_raw: list[dict] = q.get("interview_turns", [])
    turns_raw.append({"role": "user", "content": _sanitize_text(body.answer)})
    agent_count = sum(1 for t in turns_raw if t.get("role") == "agent")

    # Hard cap — finalize without an extra agent call.
    if agent_count >= _HARD_CAP:
        q["interview_turns"] = turns_raw
        row.questionnaire = q
        row.persona_status = "generating"
        await db.commit()

        from app.tasks.persona_build import generate_persona  # noqa: PLC0415

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
        InterviewerAgent().run,
        InterviewerInput(turns=conv_turns, tiktok_summary=tiktok_summary, turn_count=agent_count),
    )

    new_agent_count = agent_count + 1
    is_final = result.is_final or new_agent_count >= _HARD_CAP

    turns_raw.append({
        "role": "agent",
        "content": result.question,
        "suggestions": result.suggestions,
        "turn_label": result.turn_label,
    })
    q["interview_turns"] = turns_raw
    row.questionnaire = q

    if is_final:
        row.persona_status = "generating"
        await db.commit()

        from app.tasks.persona_build import generate_persona  # noqa: PLC0415

        generate_persona.delay(str(row.id))
        return ChatTurnResponse(
            question=result.question,
            suggestions=result.suggestions,
            is_final=True,
            turn_number=new_agent_count,
            turn_label=result.turn_label or f"~{new_agent_count} OF ~{_HARD_CAP}",
            persona_status="generating",
        )

    await db.commit()
    return ChatTurnResponse(
        question=result.question,
        suggestions=result.suggestions,
        is_final=False,
        turn_number=new_agent_count,
        turn_label=result.turn_label or f"~{new_agent_count} OF ~6",
        persona_status="chat_pending",
    )
