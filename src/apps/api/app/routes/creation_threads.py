"""Authenticated chat-first creation API.

This is the durable project shell around the existing Main Creator Agent.  The
thread only projects conversation state; PlanItem, CreatorAgentSession and Job
remain authoritative for media, strategy and rendering respectively.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS
from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.limiter import limiter
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadDeletion,
    CreationThreadEvent,
    CreationThreadUploadReservation,
    CreatorAgentEvent,
    CreatorAgentSession,
    EditArtifact,
    Job,
    JobClip,
    JobStorageDeletion,
    Persona,
    PlanItem,
    PlanItemAsset,
    TikTokPublication,
    TrainingArtifactRetentionEvent,
)
from app.routes import creator_agent
from app.routes.music_jobs import _SLOT_UPLOAD_AUDIO_CT
from app.routes.plan_items import (
    _ALLOWED_CONTENT_TYPES,
    _IMAGE_CONTENT_TYPES,
    _MAX_BYTES_PER_FILE,
    _MAX_CLIPS_PER_ITEM,
    _MAX_POOL_ASSETS,
    _MAX_POOL_IMAGE_BYTES,
    _MAX_POOL_VIDEO_BYTES,
    _MAX_VOICEOVER_BYTES,
    _OVERLAY_ALLOWED_CONTENT_TYPES,
    _pool_asset_counts_toward_capacity,
)
from app.services.creator_render_projection import build_creator_render_projection
from app.services.creator_sessions import reconcile_render_state
from app.services.generative_upload_paths import DIRECT_VOICEOVER_PREFIX
from app.services.job_phases import mark_reattempt, stamp_variant_attempt
from app.services.job_status import PLAN_ITEM_JOB_TERMINAL
from app.services.job_storage_paths import (
    JOB_OUTPUT_PREFIXES,
    job_output_path,
    normalize_job_storage_path,
    owned_job_output_path,
)

_MAX_MEDIA = _MAX_CLIPS_PER_ITEM
_MAX_EVENTS = 200
_MAX_TITLE_LENGTH = 120
_DEFAULT_TITLE = "Untitled video"
_UPLOAD_URL_TTL = timedelta(minutes=15)
_ACTIVE_AGENT_STATUSES = frozenset(
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
_ACTIVE_PUBLICATION_STATUSES = frozenset(
    {"queued", "snapshotting", "submitting", "processing", "submission_unknown"}
)
_DELETE_VARIANT_PATH_FIELDS = (
    "output_url",
    "video_path",
    "poster_path",
    "base_video_path",
    "base_poster_path",
    "subject_matte_path",
    "pre_media_overlay_video_path",
    "pre_overlay_poster_path",
    "pre_sfx_video_path",
    "visual_blocks_base_path",
    "motion_base_path",
)
_MAX_FILE_BYTES = _MAX_BYTES_PER_FILE
_MEDIA_TYPES = frozenset(
    {
        *_ALLOWED_CONTENT_TYPES,
        *_IMAGE_CONTENT_TYPES,
        "audio/x-m4a",
        *_SLOT_UPLOAD_AUDIO_CT,
    }
)
_MEDIA_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/ogg": ".ogv",
    "video/x-m4v": ".m4v",
    "video/x-msvideo": ".avi",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/aac": ".aac",
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
}
_PAPER_FORMATS = {
    "montage": "montage",
    "narrated": "narrated_planned",
    "talking_to_camera": "subtitled",
}
# Valid neutral context for a new chat project before onboarding has generated
# a personalized persona. Keep this schema-valid so render dispatch can use the
# project immediately; existing non-empty personas are never replaced.
_DEFAULT_CHAT_PERSONA = {
    "summary": "A creator making clear, engaging short-form videos.",
    "content_pillars": ["real-life moments"],
    "tone": "natural and direct",
    "audience": "people who enjoy relatable short videos",
    "posting_cadence": "as needed",
    "posts_per_week": 1,
    "sample_topics": [],
    "rationale": "A neutral starting point for your first edit.",
    "goal": "Create engaging short-form videos",
    "content_mode": "existing_footage",
    "current_situation": "",
}
_ACTION_PAYLOAD_KEYS = {
    "set_intent": {"intent"},
    "select_format": {"format"},
    "select_edit_format": {"edit_format"},
    "confirm_generation": {"session_revision", "plan_version", "plan_hash"},
    "generate": {"session_revision", "plan_version", "plan_hash", "base_generation"},
    "revise": {"intent", "session_revision", "plan_version", "plan_hash"},
    "retry": {"session_revision", "plan_version", "plan_hash", "variant_id"},
    "remove_media": {"media_id"},
    "select_variant": {"variant_id"},
}
_MAX_ACTION_PAYLOAD_BYTES = 8192

# A status question is deliberately a small, closed vocabulary.  It is not a
# second way to ask the Creator Agent for a revision: while a render is live,
# these messages only reconcile the durable render projection and report its
# state back to the chat.
_STATUS_ONLY_RE = re.compile(
    r"^(?:status|progress|update|any\s+update|how(?:'s|\s+is)\s+(?:it|the\s+render|the\s+video)"
    r"|how(?:'s|\s+is)\s+it\s+going|what(?:'s|\s+is)\s+the\s+status"
    r"|is\s+(?:it|the\s+render|the\s+video)\s+(?:ready|done|finished)"
    r"|ready\??|done\??)\s*[?!.]*$",
    re.IGNORECASE,
)
_CREATOR_PROGRESS_STATES = frozenset({"executing", "rendering", "reviewing"})


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBody(StrictBody):
    message: str | None = Field(default=None, max_length=4000)
    client_event_id: str | None = Field(default=None, max_length=160)

    @field_validator("message")
    @classmethod
    def clean_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        return value or None

    @field_validator("client_event_id")
    @classmethod
    def validate_client_event_id(cls, value: str | None) -> str | None:
        return _client_id(value) if value is not None else None


class MessageBody(StrictBody):
    message: str = Field(min_length=1, max_length=4000)
    client_event_id: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)

    @field_validator("message")
    @classmethod
    def clean_message(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("message must not be blank")
        return value

    @field_validator("client_event_id")
    @classmethod
    def validate_client_event_id(cls, value: str) -> str:
        return _client_id(value)


class ActionBody(StrictBody):
    action: Literal[
        "set_intent",
        "select_format",
        "select_edit_format",
        "confirm_generation",
        "generate",
        "revise",
        "retry",
        "remove_media",
        "select_variant",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    client_action_id: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)

    @field_validator("client_action_id")
    @classmethod
    def validate_client_action_id(cls, value: str) -> str:
        return _client_id(value)

    @model_validator(mode="after")
    def validate_payload(self) -> ActionBody:
        unexpected = set(self.payload) - _ACTION_PAYLOAD_KEYS[self.action]
        if unexpected:
            raise ValueError(f"unexpected payload fields: {', '.join(sorted(unexpected))}")
        if (
            len(json.dumps(self.payload, separators=(",", ":")).encode())
            > _MAX_ACTION_PAYLOAD_BYTES
        ):
            raise ValueError("action payload is too large")
        return self


class UploadFile(StrictBody):
    filename: str = Field(min_length=1, max_length=240)
    content_type: str = Field(min_length=1, max_length=100)
    file_size_bytes: int = Field(gt=0, le=_MAX_FILE_BYTES)
    client_upload_id: str = Field(min_length=1, max_length=160)

    @field_validator("client_upload_id")
    @classmethod
    def validate_client_upload_id(cls, value: str) -> str:
        return _client_id(value)


class UploadBody(StrictBody):
    files: list[UploadFile] = Field(min_length=1, max_length=_MAX_MEDIA)

    @model_validator(mode="after")
    def validate_client_upload_ids(self) -> UploadBody:
        upload_ids = [file.client_upload_id for file in self.files]
        if len(upload_ids) != len(set(upload_ids)):
            raise ValueError("client upload IDs must be unique")
        return self


class MediaInput(StrictBody):
    media_id: str = Field(min_length=1, max_length=160)
    # The object key is reconstructed from this opaque reservation ID.
    gcs_path: str | None = Field(default=None, max_length=500)
    kind: Literal["video", "image", "audio"]
    filename: str | None = Field(default=None, max_length=240)
    content_type: str | None = Field(default=None, max_length=100)

    @field_validator("media_id")
    @classmethod
    def validate_media_id(cls, value: str) -> str:
        return _client_id(value)


class AttachBody(StrictBody):
    media: list[MediaInput] = Field(min_length=1, max_length=_MAX_MEDIA)
    client_event_id: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)

    @field_validator("client_event_id")
    @classmethod
    def validate_client_event_id(cls, value: str) -> str:
        return _client_id(value)

    @model_validator(mode="after")
    def validate_media_batch(self) -> AttachBody:
        media_ids = [item.media_id for item in self.media]
        if len(media_ids) != len(set(media_ids)):
            raise ValueError("media IDs must be unique")
        if sum(item.kind == "audio" for item in self.media) > 1:
            raise ValueError("only one voiceover can be attached")
        return self


class ArchiveBody(StrictBody):
    client_event_id: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)

    @field_validator("client_event_id")
    @classmethod
    def validate_client_event_id(cls, value: str) -> str:
        return _client_id(value)


class RenameBody(StrictBody):
    title: str = Field(min_length=1, max_length=_MAX_TITLE_LENGTH)
    expected_revision: int = Field(ge=0)
    client_event_id: str = Field(min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("client_event_id")
    @classmethod
    def validate_client_event_id(cls, value: str) -> str:
        return _client_id(value)


class EventOut(BaseModel):
    id: str
    sequence: int
    revision: int
    role: str
    event_type: str
    content: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime


class UploadTarget(BaseModel):
    media_id: str
    upload_url: str
    # Kept as a convenience for current web clients; attach never trusts it and
    # reconstructs the key from media_id. New clients need only media_id.
    gcs_path: str
    content_type: str
    upload_headers: dict[str, str]


class CreationThreadOut(BaseModel):
    id: str
    title: str
    status: str
    revision: int
    state: dict[str, Any]
    content_plan_id: str | None = None
    active_plan_item_id: str | None = None
    active_creator_agent_session_id: str | None = None
    active_job_id: str | None = None
    creator_agent: dict[str, Any] | None = None
    job: dict[str, Any] | None = None
    media_capabilities: dict[str, Any] | None = None
    events: list[EventOut]
    created_at: datetime
    updated_at: datetime


def _enabled(user: CurrentUser) -> None:
    if not settings.creation_threads_enabled:
        raise HTTPException(status_code=404, detail="Creation chat unavailable")
    cohort = {
        entry.strip().casefold()
        for entry in settings.creation_threads_user_allowlist.split(",")
        if entry.strip()
    }
    if not cohort or "*" in cohort:
        return
    identifiers = {str(user.id).casefold(), user.email.strip().casefold()}
    if cohort.isdisjoint(identifiers):
        # Match the global capability fallback contract: callers cannot infer
        # whether the feature exists or which accounts are in the cohort.
        raise HTTPException(status_code=404, detail="Creation chat unavailable")


async def _require_creation_thread_access(user: CurrentUser) -> None:
    """Apply the rollout gate to every current and future thread endpoint."""

    _enabled(user)


router = APIRouter(dependencies=[Depends(_require_creation_thread_access)])


def _client_id(value: str) -> str:
    if not value.strip() or any(token in value for token in ("/", "\\", "..")):
        raise ValueError("identifier must be opaque")
    return value.strip()


def _available_formats() -> dict[str, str]:
    available = {"montage": "montage"}
    if settings.narrated_archetype_enabled:
        available["narrated"] = "narrated_planned"
    if settings.subtitled_archetype_enabled:
        available["talking_to_camera"] = "subtitled"
    return available


def _format_clip_limit(edit_format: str | None) -> int:
    """Mirror the format-specific limit enforced by the PlanItem setup UI."""

    return 1 if edit_format == "subtitled" else _MAX_CLIPS_PER_ITEM


def _safe_filename(filename: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).name).strip("-")
    return value[:180] or "upload"


def _media_path(user_id: uuid.UUID, thread_id: uuid.UUID, media_id: str) -> str:
    return f"users/{user_id}/creation-threads/{thread_id}/{_client_id(media_id)}"


def _media_capabilities(*, item: PlanItem, clip_count: int, visual_count: int) -> dict[str, Any]:
    """Project the existing PlanItem upload contract for chat clients."""

    return {
        "clips": {
            "current": clip_count,
            "max": _format_clip_limit(item.edit_format),
            "server_max": _MAX_CLIPS_PER_ITEM,
            "max_file_bytes": _MAX_BYTES_PER_FILE,
            "content_types": sorted(_ALLOWED_CONTENT_TYPES),
            "format": item.edit_format or "montage",
        },
        "visuals": {
            "current": visual_count,
            "max": _MAX_POOL_ASSETS,
            "max_file_bytes": {
                "image": _MAX_POOL_IMAGE_BYTES,
                "video": _MAX_POOL_VIDEO_BYTES,
            },
            "content_types": sorted(_OVERLAY_ALLOWED_CONTENT_TYPES),
        },
        "voiceover": {
            "current": 1 if item.voiceover_gcs_path else 0,
            "max": 1,
            "max_file_bytes": _MAX_VOICEOVER_BYTES,
            "content_types": sorted(_SLOT_UPLOAD_AUDIO_CT),
        },
    }


def _reserved_media_id(client_upload_id: str, content_type: str) -> str:
    """Keep object keys opaque while retaining the canonical media suffix.

    Existing render dispatch classifies still-image inputs from their object
    suffix, so dropping it would send valid photos through the video path.
    The suffix comes from the validated MIME type, never the user filename.
    """

    return f"{_client_id(client_upload_id)}{_MEDIA_EXTENSIONS[content_type]}"


def _legacy_media_path(media_id: str, paths: list[str]) -> str | None:
    if not media_id.startswith("legacy-"):
        return None
    digest = media_id.removeprefix("legacy-")
    return next(
        (
            path
            for path in paths
            if hashlib.md5(path.encode(), usedforsecurity=False).hexdigest() == digest
        ),
        None,
    )


async def _reject_input_mutation_while_rendering(db: AsyncSession, thread: CreationThread) -> None:
    item_id = getattr(thread, "active_plan_item_id", None)
    if not item_id:
        return
    # The thread projection can lag a confirmation commit.  Lock and inspect
    # PlanItem.current_job_id, which is the authoritative forward link, before
    # allowing source/format mutation.
    item = await db.get(PlanItem, item_id, with_for_update=True)
    current_job_id = getattr(item, "current_job_id", None) if item is not None else None
    if not current_job_id:
        return
    job = await db.get(Job, current_job_id, with_for_update=True)
    if job is not None and job.status not in PLAN_ITEM_JOB_TERMINAL:
        raise HTTPException(
            status_code=409,
            detail="Wait for the current render before changing its source media or format",
        )


async def _prepare_partial_variant_retry(
    db: AsyncSession,
    thread: CreationThread,
    session: CreatorAgentSession,
    user: CurrentUser,
    variant_id: str,
) -> tuple[Job, str]:
    """Fence and persist a retry for one failed variant of a partial cut.

    A partial cut is still the authoritative Job and Creator plan.  Retrying a
    single failed variant must therefore only change that variant's generation
    token; sending the plan back through ``confirm_creator_plan_controller``
    would consume another Creator attempt and enqueue all siblings again.
    """

    if not thread.active_job_id or not thread.active_plan_item_id:
        raise HTTPException(status_code=409, detail="There is no partial render to retry")
    job = await db.get(Job, thread.active_job_id, with_for_update=True)
    item = await db.get(PlanItem, thread.active_plan_item_id, with_for_update=True)
    if job is None or item is None:
        raise HTTPException(status_code=404, detail="Creation render not found")
    if (
        job.user_id != user.id
        or job.content_plan_item_id != item.id
        or item.current_job_id != job.id
        or job.mode != "content_plan"
        or (job.content_plan_ownership_epoch or 0) != session.ownership_epoch
        or session.creator_id != user.id
        or session.plan_item_id != item.id
        or session.target_job_id != job.id
    ):
        # Do not allow a thread/session to use an unrelated Job, even when the
        # caller owns both rows.  This also protects the generation fence from
        # being applied to a stale plan item.
        raise HTTPException(status_code=409, detail="That render is not part of this project")
    variants = list((job.assembly_plan or {}).get("variants") or [])
    target = next(
        (
            variant
            for variant in variants
            if isinstance(variant, dict) and variant.get("variant_id") == variant_id
        ),
        None,
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Render variant not found")
    if target.get("render_status") != "failed":
        raise HTTPException(status_code=409, detail="Only failed variants can be retried")
    # Editor saves preserve the last-good output while rendering a replacement.
    # A worker-side failure can therefore leave the parent on its previous
    # ``variants_ready`` status even though this exact variant is now failed.
    # Accept that recoverable shape as well as the orchestrator's canonical
    # partial status, but never reopen an arbitrary failed/non-ready Job.
    has_last_good_output = bool(target.get("video_path") or target.get("output_url"))
    if job.status != "variants_ready_partial" and not (
        job.status == "variants_ready" and has_last_good_output
    ):
        raise HTTPException(
            status_code=409,
            detail="A variant can only be retried on a partial ready render",
        )
    if any(
        isinstance(variant, dict)
        and variant.get("variant_id") != variant_id
        and variant.get("render_status") == "rendering"
        for variant in variants
    ):
        raise HTTPException(status_code=409, detail="Another render variant is still processing")

    render_gen_id = uuid.uuid4().hex
    stamp_variant_attempt(target)
    target["render_generation_id"] = render_gen_id
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    mark_reattempt(job)
    # The Creator session follows this exact variant generation until the
    # worker publishes ready/failed.  It is intentionally not reopened as a
    # new confirmation and does not increment render_attempts.
    session.status = "rendering"
    session.target_job_id = job.id
    session.target_variant_id = variant_id
    session.target_generation_id = render_gen_id
    session.last_error = None
    return job, render_gen_id


async def _record_partial_variant_retry_enqueue_failure(
    db: AsyncSession,
    thread: CreationThread,
    session_id: uuid.UUID,
    job_id: uuid.UUID,
    variant_id: str,
    render_gen_id: str,
    error: Exception,
) -> None:
    """Leave a scoped retry visibly retryable when the broker is unavailable."""

    # The action commit happened before enqueue by design.  Reacquire the
    # thread first, then session/job in route lock order.  A newer retry or
    # projection update must remain authoritative if one raced the broker
    # failure.
    await db.rollback()
    thread_row = (
        await db.execute(
            select(CreationThread)
            .where(
                CreationThread.id == thread.id,
                CreationThread.creator_id == thread.creator_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if thread_row is None:
        return
    session = await db.get(CreatorAgentSession, session_id, with_for_update=True)
    job = await db.get(Job, job_id, with_for_update=True)
    if session is not None and job is not None:
        variants = list((job.assembly_plan or {}).get("variants") or [])
        target = next(
            (
                variant
                for variant in variants
                if isinstance(variant, dict)
                and variant.get("variant_id") == variant_id
                and variant.get("render_generation_id") == render_gen_id
            ),
            None,
        )
        if target is not None and target.get("render_status") == "rendering":
            target["render_status"] = "failed"
            target["error"] = "The render could not be queued. Try again."
            target["error_class"] = "retry_enqueue_failed"
            job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
            if (
                session.target_job_id == job.id
                and session.target_variant_id == variant_id
                and session.target_generation_id == render_gen_id
            ):
                session.status = "failed"
                session.last_error = {
                    "code": "retry_enqueue_failed",
                    "message": str(error)[:300],
                }
    current_generation = (
        (thread_row.state or {}).get("generation") if isinstance(thread_row.state, dict) else None
    )
    same_generation = (
        isinstance(current_generation, dict)
        and thread_row.active_job_id == job_id
        and current_generation.get("job_id") == str(job_id)
        and current_generation.get("variant_id") == variant_id
        and current_generation.get("render_generation_id") == render_gen_id
        and current_generation.get("status") in {"queued", "rendering"}
    )
    if same_generation:
        thread_row.state = {
            **(thread_row.state or {}),
            "generation": {
                **current_generation,
                "status": "failed",
                "error_code": "retry_enqueue_failed",
            },
        }
    await db.commit()


async def _project(db: AsyncSession, user: CurrentUser) -> tuple[ContentPlan, PlanItem]:
    user_row = await db.get(type(user), user.id, with_for_update=True)
    if user_row is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    persona = (
        await db.execute(select(Persona).where(Persona.user_id == user.id).with_for_update())
    ).scalar_one_or_none()
    if persona is None:
        persona = Persona(
            user_id=user.id,
            questionnaire={},
            persona=dict(_DEFAULT_CHAT_PERSONA),
            persona_status="edited",
            idea_seeds=[],
        )
        db.add(persona)
        await db.flush()
    elif not persona.persona:
        # Earlier rollout builds created an empty pre-onboarding row. Repair
        # only that sentinel; a non-empty or hand-authored persona is sacred.
        persona.persona = dict(_DEFAULT_CHAT_PERSONA)
        persona.persona_status = "edited"
    plan = (
        await db.execute(
            select(ContentPlan)
            .where(ContentPlan.user_id == user.id)
            .order_by(ContentPlan.updated_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if plan is None:
        plan = ContentPlan(
            user_id=user.id,
            persona_id=persona.id,
            plan_status="edited",
            horizon_days=30,
            ownership_epoch=0,
        )
        db.add(plan)
        await db.flush()
    max_position = (
        await db.execute(
            select(func.coalesce(func.max(PlanItem.position), 0)).where(
                PlanItem.content_plan_id == plan.id
            )
        )
    ).scalar_one()
    item = PlanItem(
        content_plan_id=plan.id,
        day_index=None,
        position=int(max_position or 0) + 1,
        idea="Untitled video",
        edit_format="montage",
        montage_preset="classic",
        audio_mode="kria",
        content_mode="existing_footage",
        item_status="awaiting_clips",
        clip_gcs_paths=[],
        clip_assignments=[],
        user_edited=True,
    )
    db.add(item)
    await db.flush()
    return plan, item


async def _load(
    thread_id: str,
    user: CurrentUser,
    db: AsyncSession,
    *,
    lock: bool = False,
    creator_id: uuid.UUID | None = None,
) -> CreationThread:
    try:
        identifier = uuid.UUID(thread_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Creation thread not found") from exc
    owner_id = creator_id if creator_id is not None else user.id
    stmt = select(CreationThread).where(
        CreationThread.id == identifier, CreationThread.creator_id == owner_id
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    thread = (await db.execute(stmt)).scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=404, detail="Creation thread not found")
    return thread


async def _repair_missing_thread_job_projection(
    db: AsyncSession, thread: CreationThread, user: CurrentUser
) -> bool:
    """Recover a missing or stale thread link after the Creator commit.

    The controller owns the PlanItem/CreatorSession/Job transaction and can
    commit successfully before the projection append is interrupted.  Repair
    from the exact, owner-scoped session target even when the thread still
    points at the previous Job; never infer a Job from the user's broader Job
    collection.
    """

    if not thread.active_plan_item_id:
        return False
    if not thread.active_creator_agent_session_id:
        return False
    # Read the session target without locking it first.  Creator Agent
    # confirmation locks the authoritative PlanItem/Job graph before the
    # CreatorAgentSession; taking the session lock here first reverses that
    # order and can deadlock a confirmation racing this repair.  Re-lock and
    # revalidate the session after the authoritative rows are locked below.
    session_id = thread.active_creator_agent_session_id
    session = await db.get(CreatorAgentSession, session_id)
    target_job_id = getattr(session, "target_job_id", None) if session is not None else None
    if target_job_id is None:
        return False
    if thread.active_job_id == target_job_id:
        return False
    item = await db.get(PlanItem, thread.active_plan_item_id, with_for_update=True)
    job = await db.get(Job, target_job_id, with_for_update=True)
    plan = await db.get(ContentPlan, item.content_plan_id) if item is not None else None
    # The target may have changed while the unlocked read above was in flight.
    # Lock the session last and use its current target for every ownership
    # predicate; never link the previously observed Job after a concurrent
    # confirmation advances the session.
    session = await db.get(
        CreatorAgentSession,
        session_id,
        populate_existing=True,
        with_for_update=True,
    )
    if session is None or session.target_job_id != target_job_id:
        return False
    exact = bool(
        session is not None
        and item is not None
        and job is not None
        and plan is not None
        and session.creator_id == user.id
        and session.plan_item_id == item.id
        and session.target_job_id == job.id
        and plan.user_id == user.id
        and session.ownership_epoch == int(plan.ownership_epoch or 0)
        and item.content_plan_id == plan.id
        and item.current_job_id == job.id
        and job.user_id == user.id
        and job.content_plan_item_id == item.id
        and job.mode == "content_plan"
        and (job.content_plan_ownership_epoch or 0) == session.ownership_epoch
    )
    if not exact:
        return False
    projection = _render_projection(thread, item=item, plan=plan, session=session, job=job)
    if projection is None:
        return False
    # Commit the pointer and its read model as one validated projection. This
    # keeps a failed attempt/ownership fence from leaving a partial pointer
    # mutation for a later status path to accidentally persist.
    thread.active_job_id = job.id
    thread.state = {
        **(thread.state or {}),
        "generation": projection,
    }
    return True


async def _event_rows(db: AsyncSession, thread_id: uuid.UUID) -> list[CreationThreadEvent]:
    rows = list(
        (
            await db.execute(
                select(CreationThreadEvent)
                .where(CreationThreadEvent.thread_id == thread_id)
                .order_by(CreationThreadEvent.sequence.desc())
                .limit(_MAX_EVENTS)
            )
        )
        .scalars()
        .all()
    )
    rows.reverse()
    return rows


async def _append(
    db: AsyncSession,
    thread: CreationThread,
    *,
    event_type: str,
    role: str = "system",
    content: str | None = None,
    payload: dict[str, Any] | None = None,
    client_event_id: str | None = None,
) -> CreationThreadEvent:
    sequence = (
        int(
            (
                await db.execute(
                    select(func.coalesce(func.max(CreationThreadEvent.sequence), -1)).where(
                        CreationThreadEvent.thread_id == thread.id
                    )
                )
            ).scalar_one()
        )
        + 1
    )
    thread.revision += 1
    event = CreationThreadEvent(
        thread_id=thread.id,
        sequence=sequence,
        revision=thread.revision,
        client_event_id=client_event_id,
        role=role,
        event_type=event_type,
        content=content,
        payload=payload,
    )
    db.add(event)
    await db.flush()
    return event


async def _duplicate(
    db: AsyncSession, thread_id: uuid.UUID, client_id: str
) -> CreationThreadEvent | None:
    return (
        await db.execute(
            select(CreationThreadEvent).where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.client_event_id == client_id,
            )
        )
    ).scalar_one_or_none()


def _job_projection(job: Job | None) -> dict[str, Any] | None:
    if job is None:
        return None
    from app.routes.generative_jobs import _variants_for_response

    # Re-signing is authoritative. A storage/signing outage must be visible to
    # the client instead of returning an expired or stale playback URL.
    variants = _variants_for_response(job)
    return {
        "id": str(job.id),
        "status": job.status,
        "current_phase": job.current_phase,
        "failure_reason": job.failure_reason,
        "variants": variants,
    }


def _project_storage_path(
    value: object,
    *,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    item_id: uuid.UUID | None,
    job: Job | None = None,
) -> str | None:
    candidate = normalize_job_storage_path(value)
    if candidate is None:
        return None
    # Job-owned output keys are checked first. A linked job's candidate list
    # may also contain the PlanItem's shared source key; never classify that
    # plan prefix as a deletable job output.
    if job is not None:
        return owned_job_output_path(candidate, job)
    prefixes = [f"users/{user_id}/creation-threads/{thread_id}/"]
    if item_id is not None:
        prefixes.extend(
            [
                f"users/{user_id}/plan/{item_id}/",
                f"users/{user_id}/plan/{item_id}/pool/",
                f"users/{user_id}/plan/{item_id}/overlays/",
            ]
        )
    if any(candidate.startswith(prefix) for prefix in prefixes):
        return candidate
    return None


def _project_job_input_path(value: object, *, user_id: uuid.UUID, job_id: uuid.UUID) -> str | None:
    """Allow only exact legacy/source prefixes for an unplanned project job."""
    candidate = normalize_job_storage_path(value)
    if candidate is None:
        return None
    allowed_prefixes = (
        f"{user_id}/{job_id}/",
        f"dev-user/{job_id}/",
        f"dev-user/{user_id}/generative/",
        f"voiceover-uploads/direct/{user_id}/",
    )
    return candidate if candidate.startswith(allowed_prefixes) else None


def _project_job_storage_paths(
    job: Job,
    clips: list[Any],
    publications: list[TikTokPublication],
    *,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    item_id: uuid.UUID | None,
) -> list[str]:
    paths: list[str] = []

    def add(value: object, *, output: bool = False) -> None:
        path = _project_storage_path(
            value,
            user_id=user_id,
            thread_id=thread_id,
            item_id=item_id,
            job=job if output else None,
        )
        if path:
            paths.append(path)

    for clip in clips:
        add(clip.video_path, output=True)
        add(clip.thumbnail_path, output=True)
    plan = job.assembly_plan if isinstance(job.assembly_plan, dict) else {}
    for field in (
        "output_path",
        "video_path",
        "output_url",
        "base_output_url",
        "poster_path",
        "base_poster_path",
    ):
        add(plan.get(field), output=True)
    raw_cleanup_receipts = plan.get("_poster_backfill_cleanup_receipts")
    cleanup_receipts = (
        raw_cleanup_receipts
        if isinstance(raw_cleanup_receipts, list)
        else [raw_cleanup_receipts]
        if isinstance(raw_cleanup_receipts, dict)
        else []
    )
    for receipt in cleanup_receipts:
        if not isinstance(receipt, dict):
            continue
        add(receipt.get("old_path"), output=True)
        add(receipt.get("replacement_path"), output=True)
    variants = plan.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            for field in _DELETE_VARIANT_PATH_FIELDS:
                add(variant.get(field), output=True)
                if field == "subject_matte_path":
                    matte = _project_storage_path(
                        variant.get(field),
                        user_id=user_id,
                        thread_id=thread_id,
                        item_id=item_id,
                        job=job,
                    )
                    if matte and matte.endswith(".mp4"):
                        paths.append(f"{matte}.json")
    candidates = job.all_candidates if isinstance(job.all_candidates, dict) else {}
    clip_paths = candidates.get("clip_paths")
    if isinstance(clip_paths, list):
        for path in clip_paths:
            if output := job_output_path(path, job.id):
                paths.append(output)
            if item_id is None:
                if source := _project_job_input_path(path, user_id=user_id, job_id=job.id):
                    paths.append(source)
    preprocessed_cache = candidates.get("preprocessed_source_cache")
    if isinstance(preprocessed_cache, dict):
        processed_clip_paths = preprocessed_cache.get("processed_clip_paths")
        if isinstance(processed_clip_paths, list):
            for path in processed_clip_paths:
                add(path, output=True)
    hdr_cache = candidates.get("hdr_pretonemap_cache")
    if isinstance(hdr_cache, dict):
        processed_by_clip_id = hdr_cache.get("processed_by_clip_id")
        if isinstance(processed_by_clip_id, dict):
            for path in processed_by_clip_id.values():
                add(path, output=True)
    if item_id is None:
        add(job.raw_storage_path)
        if source := _project_job_input_path(job.raw_storage_path, user_id=user_id, job_id=job.id):
            paths.append(source)
        if source := _project_job_input_path(
            candidates.get("voiceover_gcs_path"), user_id=user_id, job_id=job.id
        ):
            paths.append(source)
    for publication in publications:
        add(publication.source_object_path, output=True)
        if publication.snapshot_object_path == f"tiktok-publish/{publication.id}.mp4":
            paths.append(publication.snapshot_object_path)
    return list(dict.fromkeys(paths))


async def _other_project_input_references(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    excluded_job_ids: set[uuid.UUID],
    excluded_item_id: uuid.UUID | None,
) -> set[str]:
    """Return owned media still referenced outside the project being deleted.

    Authenticated generative jobs may reuse any object in ``users/{user_id}/``.
    We therefore inspect the complete owner-scoped input projection instead of
    assuming a project prefix is exclusive.  This is a deletion-time scan, not
    a hot path, and failing to find a reference would be destructive.
    """

    job_query = select(Job.raw_storage_path, Job.all_candidates).where(Job.user_id == user_id)
    if excluded_job_ids:
        job_query = job_query.where(Job.id.notin_(excluded_job_ids))
    job_rows = (await db.execute(job_query)).all()

    references: set[str] = set()
    for raw_path, candidates in job_rows:
        values: list[object] = [raw_path]
        if isinstance(candidates, dict):
            values.append(candidates.get("voiceover_gcs_path"))
            clip_paths = candidates.get("clip_paths")
            if isinstance(clip_paths, list):
                values.extend(clip_paths)
        for value in values:
            if candidate := normalize_job_storage_path(value):
                references.add(candidate)

    item_query = (
        select(PlanItem.clip_gcs_paths, PlanItem.clip_assignments, PlanItem.voiceover_gcs_path)
        .join(ContentPlan, ContentPlan.id == PlanItem.content_plan_id)
        .where(ContentPlan.user_id == user_id)
    )
    if excluded_item_id is not None:
        item_query = item_query.where(PlanItem.id != excluded_item_id)
    item_rows = (await db.execute(item_query)).all()
    for clip_paths, assignments, voiceover_path in item_rows:
        values = [voiceover_path]
        if isinstance(clip_paths, list):
            values.extend(clip_paths)
        if isinstance(assignments, list):
            values.extend(
                assignment.get("gcs_path")
                for assignment in assignments
                if isinstance(assignment, dict)
            )
        for value in values:
            if candidate := normalize_job_storage_path(value):
                references.add(candidate)
    return references


def _exclude_referenced_project_storage(
    object_paths: list[str],
    object_prefixes: list[str],
    external_references: set[str],
) -> tuple[list[str], list[str]]:
    """Keep shared inputs out of both exact-key and prefix deletion work."""

    return (
        [path for path in object_paths if path not in external_references],
        [
            prefix
            for prefix in object_prefixes
            if not any(path.startswith(prefix) for path in external_references)
        ],
    )


def _creator_agent_projection(session: CreatorAgentSession | None) -> dict[str, Any] | None:
    """Expose reviewable strategy metadata, never executable plan commands."""

    if session is None:
        return None
    plan = session.active_plan if isinstance(session.active_plan, dict) else {}
    return {
        "status": session.status,
        "revision": session.revision,
        "summary": plan.get("summary"),
        "plan_hash": plan.get("plan_hash"),
        "version": plan.get("version"),
    }


def _is_status_only_message(message: str) -> bool:
    """Return whether *message* is a bounded render-status question."""

    return bool(_STATUS_ONLY_RE.fullmatch(" ".join(message.split())))


def _creator_progress_status(session: CreatorAgentSession | None) -> str | None:
    status = str(getattr(session, "status", "") or "")
    return status if status in _CREATOR_PROGRESS_STATES else None


def _generation_attempt_id(session: CreatorAgentSession | None) -> str | None:
    if session is None:
        return None
    active_plan = getattr(session, "active_plan", None)
    active = active_plan if isinstance(active_plan, dict) else {}
    raw = active.get("guided_generation_attempt_id")
    if isinstance(raw, str) and raw:
        return raw[:160]
    # Native Creator executions do not have a guided proposal id.  This stable,
    # scoped fallback still distinguishes attempts without exposing an
    # executable plan or making a mutable revision number authoritative.
    attempts = int(getattr(session, "render_attempts", 0) or 0)
    return f"{session.id}:{attempts}" if attempts > 0 else None


def _guided_generation_attempt_id(session: CreatorAgentSession | None) -> str | None:
    active_plan = getattr(session, "active_plan", None)
    active = active_plan if isinstance(active_plan, dict) else {}
    raw = active.get("guided_generation_attempt_id")
    return raw[:160] if isinstance(raw, str) and raw else None


def _job_guided_generation_attempt_id(job: Job | None) -> str | None:
    assembly_plan = getattr(job, "assembly_plan", None)
    guided = assembly_plan.get("guided_edit") if isinstance(assembly_plan, dict) else None
    raw = guided.get("generation_attempt_id") if isinstance(guided, dict) else None
    return raw[:160] if isinstance(raw, str) and raw else None


def _render_projection(
    thread: CreationThread,
    *,
    item: PlanItem | None,
    plan: ContentPlan | None,
    session: CreatorAgentSession | None,
    job: Job | None,
) -> dict[str, Any] | None:
    """Build the bounded, exact-owner render projection for a thread.

    ``state.generation`` is a durable read model, never an authority.  Every
    identifier is stamped from the same owner-scoped PlanItem graph so a stale
    thread row cannot make a user's chat poll or display another Job.
    """

    owner_id = getattr(thread, "creator_id", None) or getattr(session, "creator_id", None)
    thread_plan_id = getattr(thread, "content_plan_id", None)
    if (
        item is None
        or plan is None
        or plan.user_id != owner_id
        or (thread_plan_id is not None and thread_plan_id != plan.id)
    ):
        return None
    epoch = int(getattr(plan, "ownership_epoch", 0) or 0)
    if session is not None and (
        session.creator_id != owner_id
        or session.plan_item_id != item.id
        or int(getattr(session, "ownership_epoch", 0) or 0) != epoch
    ):
        return None
    if job is not None and (
        job.user_id != owner_id
        or job.content_plan_item_id != item.id
        or item.current_job_id != job.id
        or (
            getattr(job, "content_plan_ownership_epoch", None) is not None
            and int(getattr(job, "content_plan_ownership_epoch")) != epoch
        )
    ):
        return None
    if session is not None and job is not None:
        if session.target_job_id is not None and session.target_job_id != job.id:
            return None
        session_attempt_id = _guided_generation_attempt_id(session)
        job_attempt_id = _job_guided_generation_attempt_id(job)
        if (session_attempt_id or job_attempt_id) and session_attempt_id != job_attempt_id:
            return None

    if job is not None:
        session_id = getattr(session, "id", None) or getattr(
            thread, "active_creator_agent_session_id", None
        )
        return build_creator_render_projection(
            job_status=str(job.status),
            job_id=job.id,
            current_job_id=item.current_job_id,
            owner_id=owner_id,
            plan_item_id=item.id,
            ownership_epoch=epoch,
            session_id=session_id,
            session_revision=int(getattr(session, "revision", 0) or 0),
            attempt=int(getattr(session, "render_attempts", 0) or 0),
            generation_attempt_id=_generation_attempt_id(session),
            variant_id=getattr(session, "target_variant_id", None),
            render_generation_id=getattr(session, "target_generation_id", None),
        )
    else:
        status = _creator_progress_status(session)
        if status is not None:
            # The controller can commit its Creator execution before the
            # guided worker publishes a Job. Keep this explicit so clients do
            # not mistake an absent Job for an idle project.
            status = "preparing"
        elif str(getattr(session, "status", "") or "") == "failed":
            status = "failed"
        else:
            return None

    projection: dict[str, Any] = {
        "status": status,
        "job_id": str(job.id) if job is not None else None,
        "current_job_id": str(item.current_job_id) if item.current_job_id else None,
        "user_id": str(owner_id),
        "plan_item_id": str(item.id),
        "ownership_epoch": epoch,
    }
    if session is not None:
        session_id = getattr(session, "id", None) or getattr(
            thread, "active_creator_agent_session_id", ""
        )
        projection.update(
            {
                "session_id": str(session_id),
                "session_revision": int(getattr(session, "revision", 0) or 0),
                "attempt": int(getattr(session, "render_attempts", 0) or 0),
            }
        )
        attempt_id = _generation_attempt_id(session)
        if attempt_id:
            projection["generation_attempt_id"] = attempt_id
        target_variant_id = getattr(session, "target_variant_id", None)
        target_generation_id = getattr(session, "target_generation_id", None)
        if target_variant_id:
            projection["variant_id"] = str(target_variant_id)
        if target_generation_id:
            projection["render_generation_id"] = str(target_generation_id)
    return projection


async def _sync_render_projection(
    db: AsyncSession,
    thread: CreationThread,
    *,
    item: PlanItem | None = None,
    session: CreatorAgentSession | None = None,
    job: Job | None = None,
) -> bool:
    """Persist the current exact render read model when it has changed."""

    if item is None or session is None or (job is None and not item.current_job_id):
        item, session, job = await _load_authorized_projection_rows(db, thread)
    # A controller can commit the authoritative PlanItem→Job link before the
    # thread projection update.  Adopt that exact current Job (and only that
    # Job) so one GET repairs both the link and the status lane.
    if job is None and item is not None and item.current_job_id:
        candidate = await db.get(Job, item.current_job_id)
        if candidate is not None:
            candidate_projection = _render_projection(
                thread,
                item=item,
                plan=await db.get(ContentPlan, item.content_plan_id),
                session=session,
                job=candidate,
            )
            if candidate_projection is not None:
                job = candidate
                thread.active_job_id = candidate.id
    plan = await db.get(ContentPlan, item.content_plan_id) if item is not None else None
    projection = _render_projection(thread, item=item, plan=plan, session=session, job=job)
    if projection is None:
        return False
    if job is not None and thread.active_job_id is None:
        # The current Job passed the complete owner/item/epoch fence above;
        # repair the projection link along with its status read model.
        thread.active_job_id = job.id
    state = dict(thread.state or {})
    if state.get("generation") == projection:
        return False
    thread.state = {**state, "generation": projection}
    return True


async def _lock_reconciliation_graph(
    db: AsyncSession, thread: CreationThread
) -> CreatorAgentSession | None:
    """Lock the render graph in the same order as Creator confirmation.

    Confirmation owns PlanItem -> Job -> CreatorAgentSession.  GET is also a
    mutating reconciliation path, so it must not take the session lock first
    and then discover that the pre-Job path needs PlanItem.
    """

    item = None
    if thread.active_plan_item_id:
        item = await db.get(
            PlanItem,
            thread.active_plan_item_id,
            with_for_update=True,
            populate_existing=True,
        )
        if item is not None and item.current_job_id:
            await db.get(
                Job,
                item.current_job_id,
                with_for_update=True,
                populate_existing=True,
            )
    if not thread.active_creator_agent_session_id:
        return None
    return await db.get(
        CreatorAgentSession,
        thread.active_creator_agent_session_id,
        with_for_update=True,
        populate_existing=True,
    )


async def _load_status_reconciliation_rows(
    db: AsyncSession, thread: CreationThread, user: CurrentUser
) -> tuple[PlanItem | None, CreatorAgentSession | None, Job | None]:
    """Load the current render graph for a status refresh in lock order.

    Status messages must follow the same PlanItem -> Job -> CreatorSession
    ownership boundary as reconciliation, even when the thread projection
    still points at an older Job.
    """

    if not thread.active_plan_item_id:
        return None, None, None
    item = await db.get(
        PlanItem,
        thread.active_plan_item_id,
        with_for_update=True,
        populate_existing=True,
    )
    plan = await db.get(ContentPlan, item.content_plan_id) if item is not None else None
    if (
        item is None
        or plan is None
        or plan.user_id != user.id
        or item.content_plan_id != thread.content_plan_id
    ):
        raise HTTPException(status_code=404, detail="Creation thread not found")
    job = None
    if item.current_job_id:
        job = await db.get(Job, item.current_job_id, with_for_update=True, populate_existing=True)
        if (
            job is None
            or job.user_id != user.id
            or job.content_plan_item_id != item.id
            or (
                getattr(job, "content_plan_ownership_epoch", None) is not None
                and int(getattr(job, "content_plan_ownership_epoch"))
                != int(plan.ownership_epoch or 0)
            )
        ):
            raise HTTPException(status_code=404, detail="Creation thread not found")
    session = None
    if thread.active_creator_agent_session_id:
        session = await db.get(
            CreatorAgentSession,
            thread.active_creator_agent_session_id,
            with_for_update=True,
            populate_existing=True,
        )
        if (
            session is None
            or session.creator_id != user.id
            or session.plan_item_id != item.id
            or int(getattr(session, "ownership_epoch", 0) or 0) != int(plan.ownership_epoch or 0)
            or (
                session.target_job_id is not None
                and (job is None or session.target_job_id != job.id)
            )
        ):
            raise HTTPException(status_code=404, detail="Creation thread not found")
    return item, session, job


def _status_message(
    *, thread: CreationThread, session: CreatorAgentSession | None, job: Job | None
) -> str:
    if job is not None and job.status == "queued":
        return "Your render is queued. I’ll keep watching it and update this chat when it starts."
    if job is not None and job.status not in PLAN_ITEM_JOB_TERMINAL:
        phase = str(job.current_phase or "").replace("_", " ").strip()
        return (
            f"Kria is rendering your video ({phase}). I’ll update this chat when it’s ready."
            if phase
            else "Kria is rendering your video. I’ll update this chat when it’s ready."
        )
    creator_status = str(getattr(session, "status", "") or "")
    if creator_status == "executing":
        return "Kria is preparing the render from your confirmed direction."
    if creator_status in {"rendering", "reviewing"}:
        return "Kria is still working on the confirmed cut. I’ll update this chat when it’s ready."
    if job is not None and job.status in PLAN_ITEM_JOB_TERMINAL:
        return "That render has settled. I’ve refreshed the project status for you."
    return "I’ve refreshed your project status."


async def _load_authorized_projection_rows(
    db: AsyncSession, thread: CreationThread
) -> tuple[PlanItem | None, CreatorAgentSession | None, Job | None]:
    """Load linked rows only when every ownership edge is still coherent.

    The foreign keys on CreationThread protect row existence, not tenant
    identity.  Keep the response path fail-closed so a stale or accidentally
    cross-linked projection can never re-sign another user's render URL.
    """

    item_id = thread.active_plan_item_id
    session_id = thread.active_creator_agent_session_id
    job_id = thread.active_job_id
    if item_id is None and (session_id is not None or job_id is not None):
        raise HTTPException(status_code=404, detail="Creation thread not found")

    item = await db.get(PlanItem, item_id) if item_id is not None else None
    plan = None
    if item_id is not None:
        plan = await db.get(ContentPlan, item.content_plan_id) if item is not None else None
        if (
            item is None
            or plan is None
            or plan.user_id != thread.creator_id
            or item.content_plan_id != thread.content_plan_id
        ):
            raise HTTPException(status_code=404, detail="Creation thread not found")

    session = await db.get(CreatorAgentSession, session_id) if session_id is not None else None
    if session_id is not None and (
        session is None
        or item is None
        or plan is None
        or session.creator_id != thread.creator_id
        or session.plan_item_id != item.id
        or int(getattr(session, "ownership_epoch", 0) or 0)
        != int(getattr(plan, "ownership_epoch", 0) or 0)
        or (session.target_job_id is not None and session.target_job_id != job_id)
    ):
        raise HTTPException(status_code=404, detail="Creation thread not found")

    job = await db.get(Job, job_id) if job_id is not None else None
    if job_id is not None and (
        job is None
        or item is None
        or job.user_id != thread.creator_id
        or job.content_plan_item_id != item.id
        or item.current_job_id != job.id
        or (
            getattr(job, "content_plan_ownership_epoch", None) is not None
            and int(getattr(job, "content_plan_ownership_epoch"))
            != int(getattr(plan, "ownership_epoch", 0) or 0)
        )
    ):
        raise HTTPException(status_code=404, detail="Creation thread not found")

    return item, session, job


async def _sync_agent(db: AsyncSession, thread: CreationThread) -> None:
    if not thread.active_creator_agent_session_id:
        return
    session = await db.get(CreatorAgentSession, thread.active_creator_agent_session_id)
    if session is None:
        return
    projection = dict(thread.state or {})
    active_plan = session.active_plan if isinstance(session.active_plan, dict) else {}
    projection["creator_agent"] = {
        "status": session.status,
        "revision": session.revision,
        "summary": active_plan.get("summary"),
        "plan_hash": active_plan.get("plan_hash"),
        "version": active_plan.get("version"),
    }
    # Agent events are copied as an inert transcript projection.  Never copy
    # executable operations or external paths from model output.
    seen = set(projection.get("creator_agent_event_ids", []))
    events = (
        (
            await db.execute(
                select(CreatorAgentEvent)
                .where(CreatorAgentEvent.session_id == session.id)
                .order_by(CreatorAgentEvent.sequence)
            )
        )
        .scalars()
        .all()
    )
    for event in events:
        key = str(event.id)
        if key in seen:
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        safe_payload = {
            key: value
            for key, value in payload.items()
            if key in {"message", "summary", "plan_hash", "review", "status", "reason"}
        }
        await _append(
            db,
            thread,
            event_type=f"agent_{event.event_type}",
            role="assistant" if event.event_type.startswith("assistant") else "system",
            content=safe_payload.get("message") or safe_payload.get("summary"),
            payload=safe_payload,
        )
        seen.add(key)
    projection["creator_agent_event_ids"] = list(seen)[-100:]
    thread.state = projection


async def _response(db: AsyncSession, thread: CreationThread) -> CreationThreadOut:
    item, session, job = await _load_authorized_projection_rows(db, thread)
    media_capabilities = None
    # Unit route tests use lightweight mocks; the real response path uses the
    # authoritative PlanItem/PlanItemAsset rows for these counts.
    if isinstance(db, AsyncSession) and item is not None:
        visual_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(PlanItemAsset)
                    .where(
                        PlanItemAsset.plan_item_id == item.id,
                        PlanItemAsset.user_id == thread.creator_id,
                        _pool_asset_counts_toward_capacity(datetime.now(UTC)),
                    )
                )
            ).scalar_one()
        )
        media_capabilities = _media_capabilities(
            item=item,
            clip_count=len(item.clip_gcs_paths or []),
            visual_count=visual_count,
        )
    return CreationThreadOut(
        id=str(thread.id),
        title=getattr(thread, "title", None) or _DEFAULT_TITLE,
        status=thread.status,
        revision=thread.revision,
        state=dict(thread.state or {}),
        content_plan_id=str(thread.content_plan_id) if thread.content_plan_id else None,
        active_plan_item_id=str(thread.active_plan_item_id) if thread.active_plan_item_id else None,
        active_creator_agent_session_id=str(thread.active_creator_agent_session_id)
        if thread.active_creator_agent_session_id
        else None,
        active_job_id=str(thread.active_job_id) if thread.active_job_id else None,
        creator_agent=_creator_agent_projection(session),
        job=_job_projection(job),
        media_capabilities=media_capabilities,
        events=[
            EventOut(
                id=str(event.id),
                sequence=event.sequence,
                revision=event.revision,
                role=event.role,
                event_type=event.event_type,
                content=event.content,
                payload=event.payload if isinstance(event.payload, dict) else None,
                created_at=event.created_at,
            )
            for event in await _event_rows(db, thread.id)
        ],
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


async def _agent_message(
    request: Request, thread: CreationThread, body: MessageBody, user: CurrentUser, db: AsyncSession
) -> CreationThread:
    if not thread.active_plan_item_id:
        raise HTTPException(status_code=409, detail="Creation project is missing its draft")
    item_id = str(thread.active_plan_item_id)
    existing = (
        await db.get(CreatorAgentSession, thread.active_creator_agent_session_id)
        if thread.active_creator_agent_session_id
        else None
    )
    # A planning or render failure is terminal for that auditable Creator
    # session, not for the durable chat project. Start a fresh session on the
    # next user message so recovery does not require deleting/re-uploading the
    # project or mutating the failed session in place.
    exhausted = bool(
        existing
        and existing.status in {"awaiting_feedback", "awaiting_confirmation"}
        and int(existing.render_attempts or 0) >= int(existing.max_render_attempts or 0)
    )
    if exhausted and existing is not None:
        # The attempt cap bounds automatic work inside one confirmed turn; it
        # must not cap the lifetime of a durable chat project. A new explicit
        # creator message grants a fresh two-confirmation budget while keeping
        # the accumulated typed plan and conversation in the same audit trail.
        existing.max_render_attempts = int(existing.render_attempts or 0) + 2
    if existing is None or existing.status in {"completed", "failed", "cancelled"}:
        result = await creator_agent.start_creator_session_controller(
            request,
            item_id,
            creator_agent.StartBody(message=body.message, client_event_id=body.client_event_id),
            user,
            db,
            allow_chat=True,
        )
    else:
        result = await creator_agent.creator_session_turn_controller(
            request,
            item_id,
            creator_agent.TurnBody(
                session_id=existing.id,
                expected_revision=existing.revision,
                message=body.message,
                client_event_id=body.client_event_id,
            ),
            user,
            db,
            allow_chat=True,
        )
    thread.active_creator_agent_session_id = uuid.UUID(result.id)
    # The Creator Agent handler commits its own transaction. Reacquire the
    # thread row before appending the projected events so concurrent requests
    # cannot race on MAX(sequence).
    thread = (
        await db.execute(
            select(CreationThread)
            .where(CreationThread.id == thread.id, CreationThread.creator_id == user.id)
            .with_for_update()
        )
    ).scalar_one()
    await _sync_agent(db, thread)
    return thread


@router.get("/capabilities")
async def capabilities(user: CurrentUser) -> dict[str, Any]:
    _enabled(user)
    return {
        "formats": [
            {
                "id": key,
                "edit_format": value,
                "max_clips": _format_clip_limit(value),
            }
            for key, value in _available_formats().items()
        ],
        "media": {
            "clips": {
                "max": _MAX_CLIPS_PER_ITEM,
                "max_file_bytes": _MAX_BYTES_PER_FILE,
                "content_types": sorted(_ALLOWED_CONTENT_TYPES),
            },
            "visuals": {
                "max": _MAX_POOL_ASSETS,
                "max_file_bytes": {
                    "image": _MAX_POOL_IMAGE_BYTES,
                    "video": _MAX_POOL_VIDEO_BYTES,
                },
                "content_types": sorted(_OVERLAY_ALLOWED_CONTENT_TYPES),
            },
            "voiceover": {
                "max": 1,
                "max_file_bytes": _MAX_VOICEOVER_BYTES,
                "content_types": sorted(_SLOT_UPLOAD_AUDIO_CT),
            },
        },
    }


@router.post("", response_model=CreationThreadOut, status_code=201)
@limiter.limit("20/minute")
async def create_thread(
    request: Request,
    body: CreateBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreationThreadOut:
    _enabled(user)
    _ = request
    # Serialize creation receipts before their lookup. Otherwise concurrent
    # retries can both observe no receipt and mint two projects.
    user_row = await db.get(type(user), user.id, with_for_update=True)
    if user_row is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if body.client_event_id:
        prior = (
            await db.execute(
                select(CreationThreadEvent, CreationThread)
                .join(CreationThread, CreationThread.id == CreationThreadEvent.thread_id)
                .where(
                    CreationThread.creator_id == user.id,
                    CreationThreadEvent.client_event_id == body.client_event_id,
                    CreationThreadEvent.event_type == "thread_created",
                )
                .order_by(CreationThreadEvent.created_at.desc())
                .limit(1)
            )
        ).first()
        if prior is not None:
            event, existing = prior
            if event.content != body.message:
                raise HTTPException(status_code=409, detail="Idempotency key reused")
            return await _response(db, existing)
    plan, item = await _project(db, user)
    thread = CreationThread(
        creator_id=user.id,
        content_plan_id=plan.id,
        active_plan_item_id=item.id,
        title=(body.message[:_MAX_TITLE_LENGTH] if body.message else _DEFAULT_TITLE),
        state={
            "media": [],
            "media_count": 0,
            **({"title_source": "first_prompt"} if body.message else {}),
        },
    )
    db.add(thread)
    await db.flush()
    await _append(
        db,
        thread,
        event_type="thread_created",
        content=body.message,
        client_event_id=body.client_event_id,
    )
    await _append(
        db,
        thread,
        event_type="format_prompt",
        role="assistant",
        content="What are we making? Pick a format and we’ll shape it together.",
        payload={"kind": "select_format", "formats": _available_formats()},
    )
    if body.message:
        await _append(
            db,
            thread,
            event_type="user_message",
            role="user",
            content=body.message,
        )
    await db.commit()
    await db.refresh(thread)
    return await _response(db, thread)


@router.get("", response_model=list[CreationThreadOut])
async def list_threads(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    include_archived: bool = Query(False),
    limit: int = Query(20, ge=1, le=50),
) -> list[CreationThreadOut]:
    _enabled(user)
    stmt = select(CreationThread).where(CreationThread.creator_id == user.id)
    if not include_archived:
        stmt = stmt.where(CreationThread.status == "active")
    rows = (
        (await db.execute(stmt.order_by(CreationThread.updated_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    job_ids = {row.active_job_id for row in rows if row.active_job_id is not None}
    jobs_by_id: dict[uuid.UUID, Job] = {}
    if job_ids:
        jobs_by_id = {
            job.id: job
            for job in ((await db.execute(select(Job).where(Job.id.in_(job_ids)))).scalars().all())
        }
    session_ids = {
        row.active_creator_agent_session_id
        for row in rows
        if row.active_creator_agent_session_id is not None
    }
    session_statuses: dict[uuid.UUID, str] = {}
    if session_ids:
        session_statuses = dict(
            (
                await db.execute(
                    select(CreatorAgentSession.id, CreatorAgentSession.status).where(
                        CreatorAgentSession.id.in_(session_ids)
                    )
                )
            ).all()
        )
    # Project-rail summaries deliberately omit transcript events and variant
    # URLs. One pair of batched lookups carries lifecycle status so destructive
    # actions can be disabled accurately without an N+1 or stale assumptions.
    summaries: list[CreationThreadOut] = []
    for row in rows:
        job = jobs_by_id.get(row.active_job_id)
        variants = list((job.assembly_plan or {}).get("variants") or []) if job else []
        summaries.append(
            CreationThreadOut(
                id=str(row.id),
                title=getattr(row, "title", None) or _DEFAULT_TITLE,
                status=row.status,
                revision=row.revision,
                state=dict(row.state or {}),
                content_plan_id=str(row.content_plan_id) if row.content_plan_id else None,
                active_plan_item_id=str(row.active_plan_item_id)
                if row.active_plan_item_id
                else None,
                active_creator_agent_session_id=str(row.active_creator_agent_session_id)
                if row.active_creator_agent_session_id
                else None,
                active_job_id=str(row.active_job_id) if row.active_job_id else None,
                creator_agent=(
                    {"status": session_statuses[row.active_creator_agent_session_id]}
                    if row.active_creator_agent_session_id in session_statuses
                    else None
                ),
                job=(
                    {
                        "id": str(job.id),
                        "status": job.status,
                        "current_phase": job.current_phase,
                        "failure_reason": job.failure_reason,
                        "variants": [
                            {
                                "variant_id": variant.get("variant_id"),
                                "render_status": variant.get("render_status"),
                            }
                            for variant in variants
                            if isinstance(variant, dict)
                        ],
                    }
                    if job is not None
                    else None
                ),
                events=[],
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    return summaries


@router.get("/{thread_id}", response_model=CreationThreadOut)
async def get_thread(
    thread_id: str, user: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]
) -> CreationThreadOut:
    _enabled(user)
    thread = await _load(thread_id, user, db, lock=True)
    if await _repair_missing_thread_job_projection(db, thread, user):
        await db.commit()
        await db.refresh(thread)
    if thread.active_creator_agent_session_id:
        if isinstance(db, AsyncSession):
            session = await _lock_reconciliation_graph(db, thread)
        else:
            session = await db.get(
                CreatorAgentSession, thread.active_creator_agent_session_id, with_for_update=True
            )
        if session is not None and await reconcile_render_state(db, session):
            # Guided planning creates and binds the exact Job asynchronously.
            # Reconciliation can discover that Job after the initial repair
            # pass, so project it in the same GET instead of requiring a
            # second page reload before polling or recovery can begin.
            await _repair_missing_thread_job_projection(db, thread, user)
            await _sync_agent(db, thread)
            await db.commit()
            await db.refresh(thread)
    # Polls are also the repair loop for the projection.  The read model is
    # updated after reconciliation, so a queued/rendering Job or a session in
    # executing/reviewing with no Job is visible in one response.
    if isinstance(db, AsyncSession):
        if await _sync_render_projection(db, thread):
            await db.commit()
            await db.refresh(thread)
    return await _response(db, thread)


@router.post("/{thread_id}/messages", response_model=CreationThreadOut)
@limiter.limit("12/minute")
async def message_thread(
    request: Request,
    thread_id: str,
    body: MessageBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreationThreadOut:
    _enabled(user)
    thread = await _load(thread_id, user, db, lock=True)
    if thread.status != "active":
        raise HTTPException(status_code=409, detail="Creation thread is archived")
    duplicate = await _duplicate(db, thread.id, _client_id(body.client_event_id))
    if duplicate:
        if duplicate.event_type != "user_message" or duplicate.content != body.message:
            raise HTTPException(status_code=409, detail="Idempotency key reused")
        await db.rollback()
        return await _response(db, await _load(thread_id, user, db))
    if thread.revision != body.expected_revision:
        raise HTTPException(status_code=409, detail="Creation thread changed")
    await _append(
        db,
        thread,
        event_type="user_message",
        role="user",
        content=body.message,
        client_event_id=body.client_event_id,
    )
    state = dict(thread.state or {})
    if _is_status_only_message(body.message):
        # Status questions are intentionally inert with respect to creative
        # intent. Reconcile the exact Creator session/Job and append one
        # bounded report, but never queue a revision from words like “status?”.
        status_session = None
        status_job = None
        if isinstance(db, AsyncSession) and thread.active_plan_item_id:
            _live_item, status_session, status_job = await _load_status_reconciliation_rows(
                db, thread, user
            )
        elif thread.active_creator_agent_session_id:
            status_session = await db.get(
                CreatorAgentSession, thread.active_creator_agent_session_id, with_for_update=True
            )
            if thread.active_job_id:
                status_job = await db.get(Job, thread.active_job_id, with_for_update=True)
        if status_session is not None:
            await reconcile_render_state(db, status_session)
            if isinstance(db, AsyncSession):
                await _repair_missing_thread_job_projection(db, thread, user)
            await _sync_agent(db, thread)
        if isinstance(db, AsyncSession):
            item, session, job = await _load_authorized_projection_rows(db, thread)
            if session is not None:
                status_session = session
            if job is not None:
                status_job = job
            await _sync_render_projection(
                db,
                thread,
                item=item,
                session=status_session,
                job=status_job,
            )
        await _append(
            db,
            thread,
            event_type="status_update",
            content=_status_message(thread=thread, session=status_session, job=status_job),
            payload={
                "kind": "status",
                "status": (
                    (thread.state or {}).get("generation", {}).get("status")
                    if isinstance((thread.state or {}).get("generation"), dict)
                    else None
                ),
            },
        )
        await db.commit()
        await db.refresh(thread)
        return await _response(db, thread)
    if not state.get("intent"):
        state["intent"] = body.message[:2000]
        thread.state = state
        if (getattr(thread, "title", None) or _DEFAULT_TITLE) == _DEFAULT_TITLE and state.get(
            "title_source"
        ) != "user":
            thread.title = body.message[:_MAX_TITLE_LENGTH]
            state["title_source"] = "first_prompt"
            thread.state = state
    # Never mutate an in-flight render. Preserve the creator's message as a
    # pending revision intent even if this is an older thread whose format
    # projection has not been hydrated yet.
    if thread.active_job_id:
        current_job = await db.get(Job, thread.active_job_id)
        if current_job is not None and current_job.status not in PLAN_ITEM_JOB_TERMINAL:
            state = dict(thread.state or {})
            state["pending_revision_intent"] = body.message
            thread.state = state
            await _append(
                db,
                thread,
                event_type="revision_queued",
                content="I saved that direction and will apply it when this cut is ready.",
                payload={"job_id": str(current_job.id)},
            )
            await db.commit()
            await db.refresh(thread)
            return await _response(db, thread)
    state = dict(thread.state or {})
    # Free text before the format and footage prerequisites is durable but
    # inert. Do not invoke the Creator Agent with an incomplete manifest.
    if not state.get("edit_format"):
        await _append(
            db,
            thread,
            event_type="format_prompt",
            role="assistant",
            content="Choose a format and I’ll shape the edit around it.",
            payload={"kind": "select_format", "formats": _available_formats()},
        )
        await db.commit()
        await db.refresh(thread)
        return await _response(db, thread)
    if int(state.get("media_count", 0) or 0) <= 0:
        await _append(
            db,
            thread,
            event_type="media_prompt",
            role="assistant",
            content="Add some footage and I’ll design the first direction.",
            payload={"kind": "collect_media"},
        )
        await db.commit()
        await db.refresh(thread)
        return await _response(db, thread)
    # The existing typed controller owns LLM planning, manifest fences and
    # event semantics.  It commits its own transaction; refresh this projection
    # before returning so a model failure is visible to the client.
    if thread.active_creator_agent_session_id and thread.active_job_id:
        session = await db.get(CreatorAgentSession, thread.active_creator_agent_session_id)
        current_job = await db.get(Job, thread.active_job_id)
        if (
            session is not None
            and current_job is not None
            and current_job.status
            in {
                "done",
                "variants_ready",
                "variants_ready_partial",
            }
        ):
            await reconcile_render_state(db, session)
    # Keep the user message and Creator turn in one transaction.  The Creator
    # controller may reject a stale session/manifest with HTTP 409; rolling
    # back here prevents a message that never produced a turn from being
    # committed and duplicated on client replay.
    try:
        thread = await _agent_message(request, thread, body, user, db)
    except Exception:
        await db.rollback()
        raise
    await db.commit()
    await db.refresh(thread)
    return await _response(db, thread)


@router.post("/{thread_id}/actions", response_model=CreationThreadOut)
@limiter.limit("30/minute")
async def action_thread(
    request: Request,
    thread_id: str,
    body: ActionBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreationThreadOut:
    _enabled(user)
    owner_id = user.id
    thread = await _load(thread_id, user, db, lock=True, creator_id=owner_id)
    if body.action == "retry" and await _repair_missing_thread_job_projection(db, thread, user):
        # Commit the repaired projection before idempotency/retry handling so a
        # later 409 cannot roll the self-healing link back out.
        await db.commit()
        await db.refresh(thread)
    if thread.status != "active":
        raise HTTPException(status_code=409, detail="Creation thread is archived")
    duplicate = await _duplicate(db, thread.id, _client_id(body.client_action_id))
    if duplicate:
        expected_event_types = {f"action_{body.action}"}
        if body.action == "revise":
            expected_event_types.add("revision_requested")
        if duplicate.event_type not in expected_event_types:
            raise HTTPException(status_code=409, detail="Idempotency key reused")
        if (duplicate.payload or {}) != {"action": body.action, **body.payload}:
            raise HTTPException(status_code=409, detail="Idempotency key reused")
        await db.rollback()
        return await _response(db, await _load(thread_id, user, db))
    if thread.revision != body.expected_revision:
        raise HTTPException(status_code=409, detail="Creation thread changed")
    state = dict(thread.state or {})
    delete_path: str | None = None
    enqueue_variant_retry: tuple[str, str, str] | None = None
    if body.action in {"select_format", "select_edit_format"}:
        await _reject_input_mutation_while_rendering(db, thread)
        selected = (
            str(body.payload.get("format", body.payload.get("edit_format", "")))
            .strip()
            .lower()
            .replace("-", "_")
            .replace(" ", "_")
        )
        if selected not in _PAPER_FORMATS:
            raise HTTPException(status_code=422, detail="Unknown creation format")
        formats = _available_formats()
        if selected not in formats:
            raise HTTPException(status_code=409, detail="That format is unavailable")
        edit_format = formats[selected]
        state.update({"format": selected, "edit_format": edit_format})
        item = await db.get(PlanItem, thread.active_plan_item_id, with_for_update=True)
        if item is not None:
            item.edit_format = edit_format
            item.audio_mode = "voiceover" if edit_format in NARRATED_EDIT_FORMATS else "kria"
    elif body.action == "set_intent":
        intent = " ".join(str(body.payload.get("intent", "")).split())
        if not intent:
            raise HTTPException(status_code=422, detail="intent is required")
        state["intent"] = intent[:2000]
    elif body.action == "select_variant":
        variant = str(body.payload.get("variant_id", ""))
        if not variant:
            raise HTTPException(status_code=422, detail="variant_id is required")
        job = await db.get(Job, thread.active_job_id) if thread.active_job_id else None
        variants = (job.assembly_plan or {}).get("variants") if job else None
        selected = next(
            (
                candidate
                for candidate in (variants or [])
                if isinstance(candidate, dict)
                and candidate.get("variant_id") == variant
                and candidate.get("render_status") == "ready"
            ),
            None,
        )
        if selected is None:
            raise HTTPException(status_code=409, detail="That render variant is unavailable")
        state["selected_variant_id"] = variant[:160]
    elif body.action == "remove_media":
        await _reject_input_mutation_while_rendering(db, thread)
        try:
            media_id = _client_id(str(body.payload.get("media_id", "")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="media_id is required") from exc
        media = [entry for entry in state.get("media", []) if isinstance(entry, dict)]
        removed = next((entry for entry in media if entry.get("media_id") == media_id), None)
        if removed is None:
            raise HTTPException(status_code=409, detail="That media is no longer attached")
        item = await db.get(PlanItem, thread.active_plan_item_id, with_for_update=True)
        if item is None:
            raise HTTPException(status_code=409, detail="Creation project is missing its draft")
        paths = list(item.clip_gcs_paths or [])
        legacy_candidates = [*paths]
        if item.voiceover_gcs_path:
            legacy_candidates.append(item.voiceover_gcs_path)
        legacy_path = _legacy_media_path(media_id, legacy_candidates)
        media_path = legacy_path or _media_path(user.id, thread.id, media_id)
        item.clip_gcs_paths = [path for path in paths if path != media_path]
        item.clip_assignments = [
            assignment
            for assignment in (item.clip_assignments or [])
            if not isinstance(assignment, dict)
            or (assignment.get("gcs_path") != media_path and assignment.get("media_id") != media_id)
        ]
        if item.voiceover_gcs_path == media_path:
            item.voiceover_gcs_path = None
            if item.audio_mode == "voiceover":
                item.audio_mode = "kria"
        state["media"] = [entry for entry in media if entry.get("media_id") != media_id]
        state["media_count"] = len(state["media"])
        # Legacy paths may be shared with pre-chat drafts. Only delete objects
        # minted under this thread's exclusive prefix.
        if legacy_path is None:
            delete_path = media_path
    elif body.action == "retry" and body.payload.get("variant_id"):
        session = (
            await db.get(
                CreatorAgentSession, thread.active_creator_agent_session_id, with_for_update=True
            )
            if thread.active_creator_agent_session_id
            else None
        )
        if session is None:
            raise HTTPException(status_code=409, detail="Creation session not found")
        variant_id = str(body.payload["variant_id"]).strip()[:160]
        job, render_gen_id = await _prepare_partial_variant_retry(
            db, thread, session, user, variant_id
        )
        state["generation"] = {
            "status": "rendering",
            "job_id": str(job.id),
            "variant_id": variant_id,
            "render_generation_id": render_gen_id,
        }
        enqueue_variant_retry = (str(job.id), variant_id, render_gen_id)
    elif body.action in {"confirm_generation", "generate", "retry", "revise"}:
        session = (
            await db.get(
                CreatorAgentSession, thread.active_creator_agent_session_id, with_for_update=True
            )
            if thread.active_creator_agent_session_id
            else None
        )
        if session is None:
            raise HTTPException(
                status_code=409, detail="Ask Kria for a direction before confirming"
            )
        payload = body.payload
        planned_format = (session.active_plan or {}).get("edit_format")
        if body.action != "revise" and (
            planned_format not in set(_available_formats().values())
            or planned_format != state.get("edit_format")
        ):
            raise HTTPException(
                status_code=409,
                detail="Kria must prepare a direction in the selected Paper format",
            )
        if body.action == "retry":
            # A failed render (or a partial ready cut) is terminal at the Job
            # layer, but the exact Creator plan remains the source of truth
            # for a bounded retry. Reconcile first, then reopen confirmation
            # so the normal manifest/hash/ownership fences and dispatcher are
            # reused rather than creating a second retry state machine.
            current_job = await db.get(Job, thread.active_job_id) if thread.active_job_id else None
            if current_job is None or current_job.status not in PLAN_ITEM_JOB_TERMINAL:
                raise HTTPException(status_code=409, detail="There is no terminal render to retry")
            await reconcile_render_state(db, session)
            if session.render_attempts >= session.max_render_attempts:
                raise HTTPException(
                    status_code=409, detail="This session has used its render attempts"
                )
            if session.status in {"failed", "awaiting_feedback"}:
                session.status = "awaiting_confirmation"
        confirmation = creator_agent.ConfirmBody(
            session_id=session.id,
            expected_revision=int(payload.get("session_revision", session.revision)),
            plan_version=int(
                payload.get("plan_version", (session.active_plan or {}).get("version", 0))
            ),
            plan_hash=str(
                payload.get("plan_hash", (session.active_plan or {}).get("plan_hash", ""))
            ),
            client_event_id=body.client_action_id,
        )
        if body.action == "revise":
            revision_intent = " ".join(
                str(payload.get("intent") or state.get("intent") or "").split()
            )
            if not revision_intent:
                raise HTTPException(status_code=422, detail="revision intent is required")
            current_job = await db.get(Job, thread.active_job_id) if thread.active_job_id else None
            if current_job is not None and current_job.status in {
                "done",
                "variants_ready",
                "variants_ready_partial",
            }:
                # Reconcile the exact ready target before handing the request
                # to the Creator Agent. This is the single preparation action
                # that turns a queued revision intent into a new proposal.
                await reconcile_render_state(db, session)
                thread.state = {**state, "pending_revision_intent": revision_intent}
                await db.commit()
                thread = await _agent_message(
                    request,
                    thread,
                    MessageBody(
                        message=revision_intent,
                        client_event_id=body.client_action_id,
                        expected_revision=thread.revision,
                    ),
                    user,
                    db,
                )
                state = dict(thread.state or {})
                state.pop("pending_revision_intent", None)
                state["prepared_revision_job_id"] = str(current_job.id)
            else:
                state["pending_revision_intent"] = revision_intent
            await _append(
                db,
                thread,
                event_type="revision_requested",
                role="user",
                payload={"action": "revise", "intent": revision_intent},
                client_event_id=body.client_action_id,
            )
            thread.state = state
            await db.commit()
            # The nested Creator flow may expire server-managed columns such
            # as updated_at while it commits and rehydrates its own rows.
            # Reload before _response accesses them; otherwise the implicit
            # lazy load runs outside SQLAlchemy's greenlet context.
            await db.refresh(thread)
            return await _response(db, thread)
        result = await creator_agent.confirm_creator_plan_controller(
            str(thread.active_plan_item_id),
            confirmation,
            user,
            db,
            allow_chat=True,
        )
        # The Creator Agent controller owns and commits its transaction. Lock
        # the thread again before projecting the new Job/session so this
        # request cannot overwrite state committed by a concurrent action.
        thread = await _load(thread_id, user, db, lock=True, creator_id=owner_id)
        state = dict(thread.state or {})
        thread.active_creator_agent_session_id = uuid.UUID(result.id)
        state["generation"] = {"status": "queued", "job_id": result.current_job_id}
        thread.active_job_id = uuid.UUID(result.current_job_id) if result.current_job_id else None
        thread.state = state
        await _sync_agent(db, thread)
        state = dict(thread.state or {})
        if isinstance(db, AsyncSession):
            await _sync_render_projection(db, thread)
            # The helper updates the durable read model in-place. Refresh the
            # local action state before the common assignment below; otherwise
            # a stale pre-projection dict would clobber the fenced projection.
            state = dict(thread.state or {})
    thread.state = state
    await _append(
        db,
        thread,
        event_type=f"action_{body.action}",
        role="user",
        payload={"action": body.action, **body.payload},
        client_event_id=body.client_action_id,
    )
    await db.commit()
    await db.refresh(thread)
    if enqueue_variant_retry is not None:
        from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

        job_id, variant_id, render_gen_id = enqueue_variant_retry
        try:
            regenerate_generative_variant.delay(
                job_id,
                variant_id,
                render_gen_id=render_gen_id,
            )
        except Exception as exc:  # noqa: BLE001 - persist a retryable terminal state
            await _record_partial_variant_retry_enqueue_failure(
                db,
                thread,
                uuid.UUID(str(thread.active_creator_agent_session_id)),
                uuid.UUID(job_id),
                variant_id,
                render_gen_id,
                exc,
            )
            raise HTTPException(status_code=503, detail="Render queue unavailable") from exc
    if delete_path:
        await asyncio.to_thread(storage.delete_object_best_effort, delete_path)
    return await _response(db, thread)


@router.post("/{thread_id}/upload-urls", response_model=list[UploadTarget])
@limiter.limit("30/minute")
async def upload_urls(
    request: Request,
    thread_id: str,
    body: UploadBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[UploadTarget]:
    _enabled(user)
    _ = request
    thread = await _load(thread_id, user, db, lock=True)
    if thread.status != "active":
        raise HTTPException(status_code=409, detail="Creation thread is archived")
    item = (
        await db.get(PlanItem, thread.active_plan_item_id)
        if getattr(thread, "active_plan_item_id", None)
        else None
    )
    # Keep the reservation helper usable by migration/recovery callers that
    # only have a thread shell. Normal chat threads always have a PlanItem and
    # therefore use the canonical PlanItem namespace below.
    current_clips = len(item.clip_gcs_paths or []) if item is not None else 0
    clip_limit = (
        _format_clip_limit(getattr(item, "edit_format", None))
        if item is not None
        else _MAX_CLIPS_PER_ITEM
    )
    requested_clips = sum(1 for file in body.files if file.content_type.startswith("video/"))
    if current_clips + requested_clips > clip_limit:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This item is capped at {clip_limit} clips. "
                f"You currently have {current_clips}; you can add "
                f"{max(0, clip_limit - current_clips)} more."
            ),
        )
    target_specs: list[tuple[str, str, int, str]] = []
    for file in body.files:
        content_type = file.content_type.split(";", 1)[0].lower().strip()
        if content_type not in _MEDIA_TYPES:
            raise HTTPException(status_code=422, detail="Unsupported media type")
        if content_type in _IMAGE_CONTENT_TYPES:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Images belong in this item's Visuals pool. Upload them from the Visuals tab."
                ),
            )
        elif content_type.startswith("audio/"):
            if file.file_size_bytes > _MAX_VOICEOVER_BYTES:
                raise HTTPException(status_code=422, detail="Audio files must be 200 MB or smaller")
        elif file.file_size_bytes > _MAX_BYTES_PER_FILE:
            raise HTTPException(status_code=422, detail="Video files must be 4 GB or smaller")
        media_id = _reserved_media_id(file.client_upload_id, content_type)
        path = _media_path(user.id, thread.id, media_id)
        target_specs.append((media_id, content_type, file.file_size_bytes, path))

    media_ids = [media_id for media_id, _content_type, _size, _path in target_specs]
    reservations = {
        reservation.media_id: reservation
        for reservation in (
            (
                await db.execute(
                    select(CreationThreadUploadReservation)
                    .where(
                        CreationThreadUploadReservation.thread_id == thread.id,
                        CreationThreadUploadReservation.media_id.in_(media_ids),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
    }
    expires_at = datetime.now(UTC) + _UPLOAD_URL_TTL
    for media_id, _content_type, _size, path in target_specs:
        reservation = reservations.get(media_id)
        if reservation is None:
            db.add(
                CreationThreadUploadReservation(
                    thread_id=thread.id,
                    creator_id=user.id,
                    media_id=media_id,
                    object_path=path,
                    expires_at=expires_at,
                )
            )
        elif reservation.creator_id != user.id or reservation.object_path != path:
            raise HTTPException(status_code=404, detail="Creation thread not found")
        else:
            reservation.expires_at = expires_at
    # Commit the reservation before minting any URL. A later DELETE can now
    # reject the live PUT window or manifest the exact key after it expires.
    await db.commit()

    targets = []
    for media_id, content_type, file_size_bytes, path in target_specs:
        try:
            url = await asyncio.to_thread(
                storage.signed_put_url,
                path,
                content_type,
                file_size_bytes,
            )
        except Exception as exc:  # signer failures are retryable
            raise HTTPException(status_code=503, detail="Upload service unavailable") from exc
        targets.append(
            UploadTarget(
                media_id=media_id,
                upload_url=url,
                gcs_path=path,
                content_type=content_type,
                upload_headers={"x-goog-if-generation-match": "0"},
            )
        )
    return targets


@router.post("/{thread_id}/media", response_model=CreationThreadOut)
@limiter.limit("30/minute")
async def attach_media(
    request: Request,
    thread_id: str,
    body: AttachBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreationThreadOut:
    _enabled(user)
    _ = request
    thread = await _load(thread_id, user, db, lock=True)
    if thread.status != "active":
        raise HTTPException(status_code=409, detail="Creation thread is archived")
    duplicate = await _duplicate(db, thread.id, _client_id(body.client_event_id))
    if duplicate:
        previous_ids = {
            str(entry.get("media_id"))
            for entry in (duplicate.payload or {}).get("media", [])
            if isinstance(entry, dict)
        }
        requested_ids = {media.media_id for media in body.media}
        if previous_ids != requested_ids:
            raise HTTPException(status_code=409, detail="Idempotency key reused")
        await db.rollback()
        return await _response(db, await _load(thread_id, user, db))
    if thread.revision != body.expected_revision:
        raise HTTPException(status_code=409, detail="Creation thread changed")
    if any(media.kind == "image" for media in body.media):
        raise HTTPException(
            status_code=422,
            detail=("Images belong in this item's Visuals pool. Upload them from the Visuals tab."),
        )
    await _reject_input_mutation_while_rendering(db, thread)
    existing_state = dict(getattr(thread, "state", None) or {})
    existing_media = [entry for entry in existing_state.get("media", []) if isinstance(entry, dict)]
    existing_media_ids = {str(entry.get("media_id")) for entry in existing_media}
    if any(media.media_id in existing_media_ids for media in body.media):
        raise HTTPException(status_code=409, detail="That media is already attached")
    verified: list[dict[str, Any]] = []
    for media in body.media:
        try:
            expected_path = _media_path(user.id, thread.id, media.media_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid media identifier") from exc
        if media.gcs_path is not None and media.gcs_path != expected_path:
            raise HTTPException(status_code=422, detail="Media path does not match reservation")
        try:
            metadata = await asyncio.to_thread(storage.object_metadata, expected_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=409, detail="Upload has not finished") from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Upload verification unavailable") from exc
        content_type = (
            str(metadata.content_type or media.content_type or "").split(";", 1)[0].lower()
        )
        kind_allowed = (media.kind == "video" and content_type in _ALLOWED_CONTENT_TYPES) or (
            media.kind == "audio" and content_type in _SLOT_UPLOAD_AUDIO_CT
        )
        kind_limit = _MAX_BYTES_PER_FILE if media.kind == "video" else _MAX_VOICEOVER_BYTES
        if metadata.size <= 0 or metadata.size > kind_limit or not kind_allowed:
            raise HTTPException(status_code=422, detail="Media kind does not match upload")
        verified.append(
            {
                "media_id": _client_id(media.media_id),
                "kind": media.kind,
                "filename": media.filename,
                "content_type": content_type,
                "size_bytes": int(metadata.size),
                "_path": expected_path,
            }
        )
    item = await db.get(PlanItem, thread.active_plan_item_id, with_for_update=True)
    if item is None:
        raise HTTPException(status_code=409, detail="Creation project is missing its draft")
    # Paths live only on the authoritative PlanItem.  The thread projection
    # stores opaque IDs/kinds and a count, never external storage locations.
    existing_paths = list(item.clip_gcs_paths or [])
    requested_clips = sum(1 for source in verified if source["kind"] == "video")
    clip_limit = _format_clip_limit(getattr(item, "edit_format", None))
    if len(existing_paths) + requested_clips > clip_limit:
        raise HTTPException(
            status_code=409,
            detail=f"This item is capped at {clip_limit} clips",
        )
    requested_voiceovers = sum(1 for source in verified if source["kind"] == "audio")
    if item.voiceover_gcs_path and requested_voiceovers:
        raise HTTPException(status_code=409, detail="This item already has a voiceover")
    assignments = [
        assignment for assignment in (item.clip_assignments or []) if isinstance(assignment, dict)
    ]
    for media, source in zip(body.media, verified, strict=True):
        if source["kind"] == "video":
            existing_paths.append(source["_path"])
            assignments.append(
                {
                    "gcs_path": source["_path"],
                    "media_id": source["media_id"],
                    "kind": source["kind"],
                    "shot_id": None,
                }
            )
        existing_media.append({key: value for key, value in source.items() if key != "_path"})
    item.clip_gcs_paths = existing_paths
    item.clip_assignments = assignments
    if any(source["kind"] == "audio" for source in verified):
        audio = next(source for source in reversed(verified) if source["kind"] == "audio")
        audio_path = audio["_path"]
        item.voiceover_gcs_path = audio_path
        item.audio_mode = "voiceover"
    state = {
        **existing_state,
        "media": existing_media,
        "media_count": len(existing_media),
    }
    thread.state = state
    public_media = [
        {key: value for key, value in source.items() if key != "_path"} for source in verified
    ]
    await _append(
        db,
        thread,
        event_type="media_added",
        role="user",
        payload={"media": public_media, "media_count": state["media_count"]},
        client_event_id=body.client_event_id,
    )
    await db.execute(
        delete(CreationThreadUploadReservation).where(
            CreationThreadUploadReservation.thread_id == thread.id,
            CreationThreadUploadReservation.media_id.in_(
                [source["media_id"] for source in verified]
            ),
        )
    )
    await db.commit()
    await db.refresh(thread)
    return await _response(db, thread)


@router.patch("/{thread_id}", response_model=CreationThreadOut)
@limiter.limit("30/minute")
async def rename_thread(
    request: Request,
    thread_id: str,
    body: RenameBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreationThreadOut:
    _enabled(user)
    _ = request
    thread = await _load(thread_id, user, db, lock=True)
    duplicate = await _duplicate(db, thread.id, _client_id(body.client_event_id))
    if duplicate:
        if duplicate.event_type != "thread_renamed" or duplicate.payload != {"title": body.title}:
            raise HTTPException(status_code=409, detail="Idempotency key reused")
        await db.rollback()
        return await _response(db, await _load(thread_id, user, db))
    if thread.revision != body.expected_revision:
        raise HTTPException(status_code=409, detail="Creation thread changed")
    thread.title = body.title
    thread.state = {**dict(getattr(thread, "state", None) or {}), "title_source": "user"}
    await _append(
        db,
        thread,
        event_type="thread_renamed",
        payload={"title": body.title},
        client_event_id=body.client_event_id,
    )
    await db.commit()
    await db.refresh(thread)
    return await _response(db, thread)


@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def delete_thread(
    request: Request,
    thread_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    expected_revision: int = Query(..., ge=0),
) -> Response:
    """Permanently erase one project and every project-owned render/media row."""
    _enabled(user)
    _ = request
    try:
        identifier = uuid.UUID(thread_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Creation thread not found") from exc

    tombstone = (
        await db.execute(
            select(CreationThreadDeletion).where(CreationThreadDeletion.thread_id == identifier)
        )
    ).scalar_one_or_none()
    if tombstone is not None:
        if tombstone.creator_id != user.id:
            raise HTTPException(status_code=404, detail="Creation thread not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    thread = (
        await db.execute(
            select(CreationThread)
            .where(CreationThread.id == identifier, CreationThread.creator_id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if thread is None:
        tombstone = (
            await db.execute(
                select(CreationThreadDeletion).where(
                    CreationThreadDeletion.thread_id == identifier,
                    CreationThreadDeletion.creator_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if tombstone is not None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        raise HTTPException(status_code=404, detail="Creation thread not found")

    upload_reservations = list(
        (
            await db.execute(
                select(CreationThreadUploadReservation)
                .where(CreationThreadUploadReservation.thread_id == identifier)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if any(reservation.creator_id != user.id for reservation in upload_reservations):
        raise HTTPException(status_code=404, detail="Creation thread not found")
    if any(reservation.expires_at > datetime.now(UTC) for reservation in upload_reservations):
        raise HTTPException(status_code=409, detail="Project has an active upload")

    item = None
    plan = None
    assets: list[PlanItemAsset] = []
    artifacts: list[EditArtifact] = []
    retention_events: list[TrainingArtifactRetentionEvent] = []
    if thread.content_plan_id is not None:
        plan = await db.get(
            ContentPlan,
            thread.content_plan_id,
            populate_existing=True,
            with_for_update=True,
        )
        if plan is None or plan.user_id != user.id:
            raise HTTPException(status_code=404, detail="Creation thread not found")
        persona = await db.get(Persona, plan.persona_id, with_for_update=True)
        if persona is None or persona.user_id != user.id:
            raise HTTPException(status_code=404, detail="Creation thread not found")
        if thread.active_plan_item_id is not None:
            item = await db.get(
                PlanItem,
                thread.active_plan_item_id,
                populate_existing=True,
                with_for_update=True,
            )
            if item is None or item.content_plan_id != plan.id:
                raise HTTPException(status_code=404, detail="Creation thread not found")
            assets = list(
                (
                    await db.execute(
                        select(PlanItemAsset)
                        .where(PlanItemAsset.plan_item_id == item.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if any(asset.user_id != user.id for asset in assets):
                raise HTTPException(status_code=404, detail="Creation thread not found")
            now = datetime.now(UTC)
            if any(
                asset.status == "promoting"
                or (
                    asset.status == "preparing"
                    and asset.upload_expires_at is not None
                    and asset.upload_expires_at > now
                )
                for asset in assets
            ):
                raise HTTPException(status_code=409, detail="Project has an active upload")
            artifacts = list(
                (
                    await db.execute(
                        select(EditArtifact)
                        .where(EditArtifact.plan_item_id == item.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if any(artifact.creator_id != user.id for artifact in artifacts):
                raise HTTPException(status_code=404, detail="Creation thread not found")
            artifact_ids = [artifact.id for artifact in artifacts]
            if artifact_ids:
                retention_events = list(
                    (
                        await db.execute(
                            select(TrainingArtifactRetentionEvent)
                            .where(TrainingArtifactRetentionEvent.artifact_id.in_(artifact_ids))
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                if any(event.creator_id != user.id for event in retention_events):
                    raise HTTPException(status_code=404, detail="Creation thread not found")
                if any(event.status in {"pending", "started"} for event in retention_events):
                    raise HTTPException(
                        status_code=409,
                        detail="Project has active artifact processing",
                    )

    job_ids = {thread.active_job_id} if thread.active_job_id is not None else set()
    if item is not None:
        if item.current_job_id is not None:
            job_ids.add(item.current_job_id)
        jobs = list(
            (
                await db.execute(
                    select(Job)
                    .where(
                        or_(
                            Job.content_plan_item_id == item.id,
                            Job.id.in_(job_ids) if job_ids else False,
                        )
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
    elif job_ids:
        jobs = list(
            (await db.execute(select(Job).where(Job.id.in_(job_ids)).with_for_update()))
            .scalars()
            .all()
        )
    else:
        jobs = []
    if any(job.user_id != user.id for job in jobs):
        raise HTTPException(status_code=404, detail="Creation thread not found")
    if item is not None and item.current_job_id is not None:
        current_job = next((job for job in jobs if job.id == item.current_job_id), None)
        if current_job is None or current_job.content_plan_item_id != item.id:
            raise HTTPException(status_code=404, detail="Creation thread not found")
    job_ids.update(job.id for job in jobs)
    if thread.active_job_id:
        active_job = next((job for job in jobs if job.id == thread.active_job_id), None)
        if active_job is None:
            raise HTTPException(status_code=404, detail="Creation thread not found")
        if item is not None and getattr(active_job, "content_plan_item_id", None) not in (
            None,
            item.id,
        ):
            raise HTTPException(status_code=404, detail="Creation thread not found")

    session_ids = (
        {getattr(thread, "active_creator_agent_session_id")}
        if getattr(thread, "active_creator_agent_session_id", None) is not None
        else set()
    )
    if item is not None:
        session_ids.update(
            session_id
            for session_id in (
                await db.execute(
                    select(CreatorAgentSession.id).where(
                        CreatorAgentSession.plan_item_id == item.id
                    )
                )
            )
            .scalars()
            .all()
        )
    sessions = []
    if session_ids:
        sessions = list(
            (
                await db.execute(
                    select(CreatorAgentSession)
                    .where(CreatorAgentSession.id.in_(session_ids))
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if any(session.creator_id != user.id for session in sessions):
            raise HTTPException(status_code=404, detail="Creation thread not found")
        if thread.active_creator_agent_session_id:
            active_session = next(
                (
                    session
                    for session in sessions
                    if session.id == thread.active_creator_agent_session_id
                ),
                None,
            )
            if active_session is None or item is None or active_session.plan_item_id != item.id:
                raise HTTPException(status_code=404, detail="Creation thread not found")

    publications = []
    if job_ids:
        publications = list(
            (
                await db.execute(
                    select(TikTokPublication)
                    .where(TikTokPublication.job_id.in_(job_ids))
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if any(publication.user_id != user.id for publication in publications):
            raise HTTPException(status_code=404, detail="Creation thread not found")

    if any(job.status not in PLAN_ITEM_JOB_TERMINAL for job in jobs):
        raise HTTPException(status_code=409, detail="Project has an active render")
    if any(session.status in _ACTIVE_AGENT_STATUSES for session in sessions):
        raise HTTPException(status_code=409, detail="Project has an active creator session")
    if any(
        publication.processing_status in _ACTIVE_PUBLICATION_STATUSES
        or (publication.processing_status == "failed" and publication.retryable)
        for publication in publications
    ):
        raise HTTPException(status_code=409, detail="Project has an active publication")

    locked_thread = thread
    if locked_thread.revision != expected_revision:
        raise HTTPException(status_code=409, detail="Creation thread changed")
    if (
        locked_thread.active_plan_item_id
        and item is not None
        and locked_thread.active_plan_item_id != item.id
    ):
        raise HTTPException(status_code=409, detail="Creation thread changed")

    object_paths: list[str] = [reservation.object_path for reservation in upload_reservations]
    object_prefixes = [f"users/{user.id}/creation-threads/{identifier}/"]
    if item is not None:
        object_prefixes.extend(
            [
                f"users/{user.id}/plan/{item.id}/",
                f"dev-user/{user.id}/plan-pool-reservations/{item.id}/",
            ]
        )
        media = (locked_thread.state or {}).get("media", [])
        if isinstance(media, list):
            for entry in media:
                if isinstance(entry, dict) and entry.get("media_id"):
                    object_paths.append(
                        f"users/{user.id}/creation-threads/{identifier}/{_client_id(str(entry['media_id']))}"
                    )
        for path in item.clip_gcs_paths or []:
            if owned := _project_storage_path(
                path,
                user_id=user.id,
                thread_id=identifier,
                item_id=item.id,
            ):
                object_paths.append(owned)
        voiceover_path = normalize_job_storage_path(item.voiceover_gcs_path)
        if voiceover_path:
            if owned := _project_storage_path(
                voiceover_path,
                user_id=user.id,
                thread_id=identifier,
                item_id=item.id,
            ):
                object_paths.append(owned)
            elif voiceover_path.startswith(f"{DIRECT_VOICEOVER_PREFIX}{user.id}/"):
                shared_voiceover = (
                    await db.execute(
                        select(PlanItem.id)
                        .where(
                            PlanItem.voiceover_gcs_path == voiceover_path,
                            PlanItem.id != item.id,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if shared_voiceover is None:
                    object_paths.append(voiceover_path)
        for asset in assets:
            for path in (asset.gcs_path, asset.preview_gcs_path):
                candidate = normalize_job_storage_path(path)
                staging_prefix = f"dev-user/{user.id}/plan-pool-reservations/{item.id}/"
                if candidate and candidate.startswith(staging_prefix):
                    object_paths.append(candidate)
                    continue
                if owned := _project_storage_path(
                    path,
                    user_id=user.id,
                    thread_id=identifier,
                    item_id=item.id,
                ):
                    object_paths.append(owned)
        retention_by_artifact: dict[uuid.UUID, list[TrainingArtifactRetentionEvent]] = {}
        for event in retention_events:
            retention_by_artifact.setdefault(event.artifact_id, []).append(event)
        for artifact in artifacts:
            artifact_prefix = f"users/{user.id}/edit-feedback/{artifact.id}/"
            object_prefixes.append(artifact_prefix)
            for path in [
                artifact.storage_path,
                *(event.storage_path for event in retention_by_artifact.get(artifact.id, [])),
            ]:
                candidate = normalize_job_storage_path(path)
                if candidate and candidate.startswith(artifact_prefix):
                    object_paths.append(candidate)

    for job in jobs:
        object_prefixes.extend(prefix.format(job_id=job.id) for prefix in JOB_OUTPUT_PREFIXES)
        clips = list(
            (await db.execute(select(JobClip).where(JobClip.job_id == job.id))).scalars().all()
        )
        project_pubs = [publication for publication in publications if publication.job_id == job.id]
        object_paths.extend(
            _project_job_storage_paths(
                job,
                clips,
                project_pubs,
                user_id=user.id,
                thread_id=identifier,
                item_id=item.id if item is not None else None,
            )
        )

    external_references = await _other_project_input_references(
        db,
        user_id=user.id,
        excluded_job_ids=job_ids,
        excluded_item_id=item.id if item is not None else None,
    )
    object_paths, object_prefixes = _exclude_referenced_project_storage(
        object_paths,
        object_prefixes,
        external_references,
    )
    object_paths = list(dict.fromkeys(object_paths))
    db.add(CreationThreadDeletion(thread_id=identifier, creator_id=user.id))
    deletion_outbox_id: uuid.UUID | None = None
    object_prefixes = list(dict.fromkeys(object_prefixes))
    if object_paths or object_prefixes:
        deletion_outbox_id = uuid.uuid4()
        db.add(
            JobStorageDeletion(
                # JobStorageDeletion has no FK by design; reusing its durable
                # exact-key sweeper keeps project cleanup retryable after commit.
                id=deletion_outbox_id,
                job_id=identifier,
                object_paths=object_paths,
                object_prefixes=object_prefixes,
            )
        )

    if item is not None:
        item.current_job_id = None
    if jobs:
        for job in jobs:
            job.content_plan_item_id = None
        await db.execute(delete(TikTokPublication).where(TikTokPublication.job_id.in_(job_ids)))
        await db.execute(delete(JobClip).where(JobClip.job_id.in_(job_ids)))
        for job in jobs:
            await db.delete(job)
    if item is not None:
        await db.execute(delete(PlanItem).where(PlanItem.id == item.id))
    elif session_ids:
        await db.execute(delete(CreatorAgentSession).where(CreatorAgentSession.id.in_(session_ids)))
    # Use a direct parent delete so the 0092 append-only trigger permits the
    # database FK cascade to remove transcript events after the parent vanishes.
    await db.execute(delete(CreationThread).where(CreationThread.id == identifier))
    await db.commit()

    if deletion_outbox_id is not None:
        from app.tasks.account_lifecycle import purge_job_storage  # noqa: PLC0415

        try:
            purge_job_storage.apply_async(args=[str(deletion_outbox_id)])
        except Exception:  # noqa: BLE001 — Beat sweeper retains the manifest
            pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{thread_id}/archive", response_model=CreationThreadOut)
@limiter.limit("20/minute")
async def archive_thread(
    request: Request,
    thread_id: str,
    body: ArchiveBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreationThreadOut:
    _enabled(user)
    _ = request
    thread = await _load(thread_id, user, db, lock=True)
    duplicate = await _duplicate(db, thread.id, _client_id(body.client_event_id))
    if duplicate:
        if duplicate.event_type != "thread_archived":
            raise HTTPException(status_code=409, detail="Idempotency key reused")
        await db.rollback()
        return await _response(db, await _load(thread_id, user, db))
    if thread.revision != body.expected_revision:
        raise HTTPException(status_code=409, detail="Creation thread changed")
    thread.status = "archived"
    await _append(
        db,
        thread,
        event_type="thread_archived",
        content="This project is archived.",
        client_event_id=body.client_event_id,
    )
    await db.commit()
    await db.refresh(thread)
    return await _response(db, thread)
