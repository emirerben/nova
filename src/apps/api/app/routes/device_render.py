"""Owner- and revision-fenced final uploads from the phone renderer."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from google.api_core.exceptions import NotFound
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.db_locks import CONTENT_PLAN_LOCK
from app.kria.device_render import (
    BRAND_TAIL_SECONDS,
    DeviceAssetDownloadBody,
    DeviceAssetDownloadOut,
    DeviceExportCompleteBody,
    DeviceExportCompleteOut,
    DeviceExportReservationBody,
    DeviceExportReservationOut,
    DeviceRenderFailureBody,
    DeviceRenderFailureOut,
    DeviceRenderIdentity,
    DeviceRenderStatus,
    DeviceRenderStatusOut,
    DeviceRetryBody,
    DeviceRetryOut,
    finished_durations,
    require_current_request,
)
from app.kria.render_assets import (
    LibraryRenderAsset,
    ProcessedAudioRenderAsset,
    SongRenderAsset,
    VisualRenderAsset,
    VoiceoverRenderAsset,
)
from app.limiter import limiter
from app.models import (
    ContentPlan,
    Job,
    MediaAssetSource,
    PlanItem,
    PlanItemAsset,
    TemporaryMediaUpload,
)
from app.routes.generative_jobs import PLAYBACK_URL_TTL_MIN
from app.schemas.edit_proposal import NarrationTrack
from app.services.content_plan_persona import PlanPersonaOwnershipError, load_owned_plan_persona
from app.services.creator_execution_contract import narration_matches_item
from app.services.creator_render_contract import CreatorRenderContractError
from app.services.device_narration_binding import authorized_device_narration
from app.services.device_render import (
    CONTRACT_DECLINE_FIELD,
    PAUSED_FIELD,
    DeviceRecipeSuperseded,
    apply_device_failure_variant_update,
    apply_retry_variant_reset,
    approved_export_sources,
    checked_export_report,
    clear_device_pause,
    device_contract_blocks_retry,
    device_paused,
    device_record,
    device_status,
    export_source_digest,
    fail_first_render_on_refusal,
    has_accepted_artifact,
    mark_device_failed,
    mark_device_paused,
    record_contract_decline,
    retry_device_render,
    save_device_record,
    touch_device_poll,
    verify_device_record_contract,
)
from app.services.guided_speech_cleanup import (
    narration_speech_cleanup,
    require_guided_cleanup_binding,
)
from app.services.job_storage_paths import project_media_reference_lock_key
from app.services.native_activity import record_native_activity
from app.services.phone_rollout import (
    card_tilt_client_supported,
    clip_backdrop_media_client_supported,
    clip_tilt_client_supported,
    freehand_drawing_client_supported,
    freeze_frame_client_supported,
    recipe_has_clip_backdrop_media,
    recipe_has_clip_tilt,
    recipe_has_freeze_frame,
    recipe_has_single_word_text,
    recipe_has_tilted_card,
    single_word_text_client_supported,
    speaker_background_client_supported,
    text_behind_subject_phone_client_supported,
    text_bend_client_supported,
)
from app.services.pipeline_trace import pipeline_trace_for, record_pipeline_event
from app.services.render_library import catalog_path, inspect_library_asset
from app.services.speech_cleanup import SpeechCleanupFailure
from app.services.speech_cleanup_outcome import build_preflight_public_outcome
from app.services.template_poster import upload_video_poster

log = structlog.get_logger()
router = APIRouter()

# GET /device-render is polled continuously by the phone client and the item
# page while a render is in flight. Throttle the last_polled_at write (the
# reaper's staleness signal — app/tasks/device_render_reaper.py) to at most
# once per this many seconds per record so polling never turns into a write
# on every request.
_POLL_TOUCH_THROTTLE_S = 60


class _DeviceConflict(HTTPException):
    """A 409 with a machine code, wire-compatible with every older client.

    The default FastAPI handler still sends ``{"detail": "<legacy text>"}``, so a
    route that does not opt in is byte-identical. Routes the phone polls answer with
    ``_conflict_response`` instead, which adds the runtime-v2 ``problem`` envelope
    (``code``, ``message`` and extras such as ``active_job_id``) beside that detail.
    """

    def __init__(self, code: str, detail: str, *, status_code: int = 409, **extra: str) -> None:
        super().__init__(status_code, detail)
        self.code = code
        self.extra = {key: value for key, value in extra.items() if value}


def _conflict_response(conflict: _DeviceConflict) -> JSONResponse:
    return JSONResponse(
        status_code=conflict.status_code,
        headers=conflict.headers,
        content={
            "detail": conflict.detail,
            "problem": {"code": conflict.code, "message": conflict.detail, **conflict.extra},
        },
    )


def _rollout_paused() -> _DeviceConflict:
    """Phone rendering is rolled back for this account while a recipe is pinned.

    A temporary hold, never the phone's fault: a 503 the phone treats as "wait and
    try again" (it parks the render instead of reporting a device failure), with the
    same legacy detail text older builds already read and a machine code beside it.
    """
    conflict = _DeviceConflict(
        "phone_rendering_paused", "Phone rendering is unavailable", status_code=503
    )
    conflict.headers = {"Retry-After": "300"}
    return conflict


def _superseded(detail: str, active_job_id: object = None) -> _DeviceConflict:
    return _DeviceConflict(
        "superseded", detail, active_job_id=str(active_job_id) if active_job_id else ""
    )


async def _owned_job(db: AsyncSession, user_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    # The owner advisory lock is shared with temporary-upload cleanup/account erasure.
    await db.execute(select(func.pg_advisory_xact_lock(project_media_reference_lock_key(user_id))))
    job = (
        await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, "Job not found")
    initial_item_id = job.content_plan_item_id
    locked_epoch = None
    if initial_item_id is not None:
        item = await db.get(PlanItem, job.content_plan_item_id)
        if item is None:
            raise _superseded("Project changed")
        plan = await db.get(
            ContentPlan,
            item.content_plan_id,
            with_for_update=CONTENT_PLAN_LOCK,
            populate_existing=True,
        )
        if plan is None or plan.user_id != user_id:
            raise HTTPException(404, "Job not found")
        try:
            await load_owned_plan_persona(db, plan, for_update=True)
        except PlanPersonaOwnershipError as exc:
            raise _superseded("Project changed") from exc
        item = await db.get(PlanItem, item.id, with_for_update=True, populate_existing=True)
        if (
            item is None
            or item.current_job_id != job_id
            or int(job.content_plan_ownership_epoch or 0) != int(plan.ownership_epoch or 0)
        ):
            # A newer Job owns this project: point the phone at it instead of failing.
            raise _superseded(
                "Project changed",
                item.current_job_id if item is not None and item.current_job_id != job_id else None,
            )
        locked_epoch = int(plan.ownership_epoch or 0)
    job = await db.get(Job, job_id, with_for_update=True, populate_existing=True)
    if job is None or job.user_id != user_id:
        raise HTTPException(404, "Job not found")
    if job.content_plan_item_id != initial_item_id or (
        locked_epoch is not None and int(job.content_plan_ownership_epoch or 0) != locked_epoch
    ):
        raise _superseded("Project changed")
    if job.status == "cancelled":
        raise _DeviceConflict("render_cancelled", "Render cancelled")
    return job


class _ContractRefusal(_DeviceConflict):
    """The recorded recipe no longer proves its approved creator requirements.

    Still the same 409 every caller already maps; the cause rides along so the
    publication path can record the typed refusal (`_refuse_publication`). A newer
    approved attempt (`DeviceRecipeSuperseded`) is not a refusal of the edit: its
    code is ``superseded`` and callers never record it as a decline.
    """

    def __init__(self, cause: CreatorRenderContractError, variant_id: str) -> None:
        super().__init__(
            "superseded" if isinstance(cause, DeviceRecipeSuperseded) else "contract_changed",
            "Device recipe changed",
        )
        self.cause = cause
        self.variant_id = variant_id

    @property
    def superseded(self) -> bool:
        return isinstance(self.cause, DeviceRecipeSuperseded)


async def _refuse_publication(
    db: AsyncSession,
    job: Job,
    refusal: _ContractRefusal,
    *,
    stage: str = "publication",
) -> None:
    """Persist a refused publication WITHOUT touching the last accepted artifact.

    The typed decline is written beside the record's intact state, and a record
    still waiting on the phone moves to `needs_attention`. An EDIT (the variant
    already has an accepted artifact) leaves the variant (video, poster, URL, `ok`)
    untouched: the previously accepted output stays live. A FIRST render has nothing
    to keep, so the variant and job are failed visibly with the typed decline.
    """
    if refusal.superseded:
        # A newer approved attempt owns the variant. Nothing about this edit was
        # refused: leave the record and the last good output alone (the caller
        # answers `superseded`, and the reaper skips a record the fence moved past).
        return
    decline = record_contract_decline(job, refusal.variant_id, refusal.cause, stage=stage)
    if decline is not None and not has_accepted_artifact(job, refusal.variant_id):
        # A first render has no last good output to keep: the job must fail visibly.
        fail_first_render_on_refusal(job, refusal.variant_id, decline)
    try:
        mark_device_failed(
            job,
            refusal.variant_id,
            reason_code="export_failed" if stage == "actual_export" else "contract_changed",
            detail=str(refusal.cause)[:500],
        )
    except (KeyError, ValueError):
        pass  # already terminal (published / needs_attention): the decline is enough
    await record_native_activity(db, job, refusal.variant_id)
    await db.commit()
    await asyncio.to_thread(_nudge_kria_observer, job.id)


def _export_text_notes(job: Job, verification_report: dict | None) -> list[str]:
    """A creator-facing note when the export's on-screen text could not be confirmed.

    The phone's export check can reject a text layer (the OCR read too few of its words)
    while the finished video still ships. That stays advisory: nothing is withheld or
    retried, but the creator is told instead of left to find it. Says "couldn't confirm",
    never "is wrong" (the check reads pixels, it does not see the video). No failed text
    check, or the switch off => no note.
    """
    if not settings.export_text_note_enabled or not isinstance(verification_report, dict):
        return []
    if verification_report.get("status") not in {"rejected", "partial"}:
        return []
    times = [
        float(row["at_s"])
        for row in verification_report.get("checks", [])
        if isinstance(row, dict)
        and row.get("kind") == "text"
        and row.get("status") == "failed"
        and row.get("expected_visible") is True
        and isinstance(row.get("at_s"), int | float)
    ]
    if not times:
        return []
    from app.kria.reply_language import reply_language_for, say  # noqa: PLC0415

    language = (job.assembly_plan or {}).get("creator_reply_language")
    at = min(times)
    with reply_language_for(language if isinstance(language, str) else None):
        return [
            say(
                en=(
                    f"I couldn't confirm all the on-screen text in the finished video was "
                    f"readable (around {at:.0f}s). If a line looks off, tell me and I'll fix it."
                ),
                tr=(
                    f"Bitmiş videodaki ekran yazılarının hepsinin okunaklı olduğunu "
                    f"doğrulayamadım (yaklaşık {at:.0f}. saniye). Bir satır yanlış görünüyorsa "
                    f"söyle, düzelteyim."
                ),
            )
        ]


def _record_export_advisory(
    job_id: uuid.UUID, identity: DeviceRenderIdentity, warning: dict
) -> None:
    """Quality checks never withhold a finished export; they leave a trace instead."""
    log.warning(
        "device_export_published_with_advisory",
        job_id=str(job_id),
        variant_id=identity.variant_id,
        **warning,
    )
    with pipeline_trace_for(job_id):
        record_pipeline_event(
            "device_export",
            "verification_advisory",
            {"variant_id": identity.variant_id, **warning},
        )


def _nudge_kria_observer(job_id: uuid.UUID) -> None:
    """The phone's report just settled this render: post Kria's chat card now."""
    from app.tasks.kria_runtime import nudge_kria_observer  # noqa: PLC0415

    nudge_kria_observer(job_id)


def _record(
    job: Job,
    identity: DeviceRenderIdentity,
    *,
    contract_advisory: list[CreatorRenderContractError] | None = None,
) -> tuple[dict, DeviceRenderStatus]:
    """Load the pinned record.

    With ``contract_advisory`` (export completion only), a changed approved
    contract is appended there instead of refusing: the phone already finished a
    video, so only a superseded attempt withholds it.
    """
    try:
        record = device_record(job, identity.variant_id)
        status = device_status(job, identity.variant_id)
        require_current_request(status, identity)
    except KeyError as exc:
        raise HTTPException(404, "Device recipe unavailable") from exc
    except (ValueError, CreatorRenderContractError) as exc:
        raise _superseded("Device recipe changed") from exc
    if identity.job_id != job.id:
        raise _superseded("Device recipe changed")
    variant = next(
        (
            v
            for v in (job.assembly_plan or {}).get("variants", [])
            if v.get("variant_id") == identity.variant_id
        ),
        None,
    )
    # The generation fence runs before the contract recheck: a record the variant has
    # moved past is superseded, never a refusal to record against the newer edit.
    if variant is None or str(variant.get("render_generation_id") or "") != record.get(
        "base_generation"
    ):
        raise _superseded("Device recipe changed")
    if variant.get("editor_state") == "empty":
        # The pinned device record is retained internally for source provenance,
        # but an empty draft has no renderable program to poll, retry, upload,
        # fail, or complete.
        raise _DeviceConflict("editor_empty", "Editor draft has no video to render")
    try:
        # A record that was pinned with creator authority cannot become a
        # legacy record merely because that root authority disappeared.
        verify_device_record_contract(job, record, status)
    except DeviceRecipeSuperseded as exc:
        raise _ContractRefusal(exc, identity.variant_id) from exc
    except CreatorRenderContractError as exc:
        if contract_advisory is None:
            raise _ContractRefusal(exc, identity.variant_id) from exc
        contract_advisory.append(exc)
    return record, status


_REPAIR_RENEWAL = timedelta(minutes=30)


async def _expire_repair_permission(
    db: AsyncSession,
    job: Job,
    record: dict,
    status: DeviceRenderStatus,
) -> bool:
    """Renew an automatic-repair grant that lapsed while the SAME recipe was in flight.

    The grant was consented for one exact recipe. While the record still carries
    that recipe (same digest), a lapse is only the clock running out mid-delivery,
    so it renews for another window instead of failing the creator's approved edit.
    Only a grant that no longer matches the pinned recipe stops (``True``).
    """
    if status.request.recovery is None or status.phase in {"published", "needs_attention"}:
        return False
    binding = record.get("repair_authority") or {}
    raw = binding.get("expires_at")
    now = datetime.now(UTC)
    try:
        live = datetime.fromisoformat(raw) > now
    except (ValueError, TypeError):
        live = False
    if live:
        return False
    if (
        isinstance(binding, dict)
        and (binding.get("identity") or {}).get("recipe_digest")
        == status.request.identity.recipe_digest
    ):
        # Mutate in place: completion keeps saving this same dict after the check.
        # The caller's own commit persists it; a renewal lost to a rollback is simply
        # granted again at the next check, since it depends only on the recipe.
        binding["expires_at"] = (now + _REPAIR_RENEWAL).isoformat()
        binding["renewals"] = int(binding.get("renewals") or 0) + 1
        record["repair_authority"] = binding
        save_device_record(job, status.request.identity.variant_id, record)
        return False
    record["repair_stopped"] = "approval_expired"
    save_device_record(job, status.request.identity.variant_id, record)
    mark_device_failed(
        job,
        status.request.identity.variant_id,
        reason_code="contract_changed",
        detail=(
            "This repair no longer matches your approved edit. Your edit is saved; "
            "review it in chat to continue."
        ),
    )
    await record_native_activity(db, job, status.request.identity.variant_id)
    await db.commit()
    return True


@router.get("/jobs/{job_id}/device-render", response_model=DeviceRenderStatusOut)
async def get_device_render(
    job_id: uuid.UUID,
    user: CurrentUser,
    variant_id: str = Query(..., min_length=1, max_length=160),
    db: AsyncSession = Depends(get_db),
) -> DeviceRenderStatusOut | JSONResponse:
    try:
        return await _device_render_status(db, user.id, job_id, variant_id)
    except _DeviceConflict as conflict:
        return _conflict_response(conflict)


async def _settle_recorded_refusal(
    db: AsyncSession, job: Job, variant_id: str, identity: DeviceRenderIdentity
) -> tuple[dict, DeviceRenderStatus]:
    """GET found a contract refusal: persist it once, then report it as a real state.

    A superseded record answers ``superseded`` (the client follows the newer
    attempt). Any other refusal is written exactly like publication writes it
    (typed decline, ``needs_attention`` with ``contract_changed``, a first render
    failed visibly, an edit's last good output kept), so polling settles instead of
    answering the same bare 409 forever and starving the reaper (S4-o).
    """
    try:
        return _record(job, identity)
    except _ContractRefusal as refusal:
        if refusal.superseded:
            raise
        record = device_record(job, variant_id)
        if (record.get("status") or {}).get("phase") in {"awaiting_device", "syncing"}:
            await _refuse_publication(db, job, refusal, stage="poll")
        elif CONTRACT_DECLINE_FIELD not in record:
            record_contract_decline(job, variant_id, refusal.cause, stage="poll")
            await db.commit()
        return device_record(job, variant_id), device_status(job, variant_id)


# The client protocol of the build that declined a recipe as `renderer_outdated`.
# Only a newer build can render it, so `/retry` from the same build would only loop.
DECLINED_PROTOCOL_FIELD = "declined_client_protocol"
# Why that build was too old: "floor" when it was below a protocol floor the server checks
# for this recipe (`_client_update_detail`), "renderer" for a gap only the phone can see
# (its renderer or recipe-schema version). Only a floor gap is re-pinned automatically: for
# it the server knows whether a newer build passes; a renderer gap is the phone's own call
# (it shows "Try again" with `renders_locally` once it can render the recipe).
DECLINED_GAP_FIELD = "declined_gap"
# The newest client protocol that read this record's recipe without a 426 (it can decode
# every feature the server gates by protocol). A `renderer_outdated` decline from an older
# build never fails a record such a device holds, and the value survives a re-pin of the
# same recipe, so two devices on different builds cannot fail and re-pin it in a loop.
READER_PROTOCOL_FIELD = "max_reader_client_protocol"


def _caller_protocol() -> int:
    from app.services.client_protocol import current_client_protocol  # noqa: PLC0415

    return current_client_protocol() or 0


def _caller_is_newer_than_decliner(record: dict) -> bool:
    """True when this request comes from a build newer than the one that said "outdated".

    A record declined before the field existed has no protocol to compare: let the
    caller try (the old behavior), since refusing would leave it no way forward.
    """
    declined = record.get(DECLINED_PROTOCOL_FIELD)
    if not isinstance(declined, int) or isinstance(declined, bool):
        return True
    return _caller_protocol() > declined


def _reader_protocol(record: dict) -> int:
    value = record.get(READER_PROTOCOL_FIELD)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _client_update_detail(job: Job, record: dict, status: DeviceRenderStatus) -> dict | None:
    """The 426 detail this caller gets for the pinned recipe, or None when it decodes it.

    One place for every feature the server gates by client protocol, so the GET can
    check them BEFORE it re-pins anything for a build (`_auto_resume_updated`): a build
    newer than the one that declined is not necessarily new enough for the recipe.
    """
    protocol = _caller_protocol()
    recipe = status.request.recipe
    from app.agents._schemas.text_animation_phases import DESIGNED_TEXT_EDGES

    if protocol < 23 and any(
        layer.animation_phases is not None
        and DESIGNED_TEXT_EDGES.intersection(
            (layer.animation_phases.entrance, layer.animation_phases.exit)
        )
        for layer in getattr(recipe, "text_layers", None) or []
    ):
        return {
            "code": "client_update_required",
            "feature": "designed_text_motion",
            "minimum_client_protocol": 23,
        }
    if "actual_export_policy" in record and protocol < 13:
        return {
            "code": "client_update_required",
            "feature": "export_verification",
            "minimum_client_protocol": 13,
        }
    from app.services.phone_text_styling import (  # noqa: PLC0415
        PHONE_TEXT_STYLE_FIELD,
        require_phone_text_style_client,
    )

    try:
        require_phone_text_style_client(
            (job.assembly_plan or {}).get(PHONE_TEXT_STYLE_FIELD), require_header=True
        )
    except HTTPException as exc:
        if isinstance(exc.detail, dict):
            return dict(exc.detail)
        return {"code": "client_update_required", "feature": "text_preferences"}
    if getattr(recipe, "drawing_layers", None) and not freehand_drawing_client_supported():
        # KRI-589: an older build rejects `drawingLayers` as an unknown recipe key and would
        # FAIL the job instead of rendering. Tell it to update; the pinned recipe is untouched.
        return {
            "code": "client_update_required",
            "feature": "drawing",
            "minimum_client_protocol": settings.freehand_drawing_min_client_protocol,
        }
    if not text_bend_client_supported() and any(
        layer.bend for layer in getattr(recipe, "text_layers", None) or []
    ):
        # KRI-646: same for bent text; an older build cannot decode `bend`.
        return {
            "code": "client_update_required",
            "feature": "text_bend",
            "minimum_client_protocol": settings.text_bend_min_client_protocol,
        }
    if not text_behind_subject_phone_client_supported() and any(
        layer.behind_subject for layer in getattr(recipe, "text_layers", None) or []
    ):
        # Text behind people: an older build rejects `behind_subject` as an unknown recipe key
        # and would FAIL the job. Compile already strips the flag for such a build's own saves;
        # this covers a recipe a newer build or the worker pinned before this build read it.
        return {
            "code": "client_update_required",
            "feature": "text_behind_subject",
            "minimum_client_protocol": settings.text_behind_subject_phone_min_client_protocol,
        }
    if (
        getattr(recipe, "speaker_background", None) is not None
        and not speaker_background_client_supported()
    ):
        # KRI-1117: an older build rejects `speaker_background` as an unknown recipe key and
        # would FAIL the job. Compile strips the lane for a build below the minimum, so this
        # covers a recipe a newer build or the worker pinned before this build read it.
        return {
            "code": "client_update_required",
            "feature": "speaker_background",
            "minimum_client_protocol": settings.speaker_background_min_client_protocol,
        }
    if not card_tilt_client_supported() and recipe_has_tilted_card(recipe):
        # KRI-1141: a build below protocol 30 decodes the `clipTilt` token a tilted card carries
        # but draws the card upright. Checked before the clip gate so the answer names the newer
        # of the two minimums.
        return {
            "code": "client_update_required",
            "feature": "card_tilt",
            "minimum_client_protocol": settings.media_card_tilt_min_client_protocol,
        }
    if not clip_tilt_client_supported() and recipe_has_clip_tilt(recipe):
        # KRI-1134: an older build cannot decode the `clipTilt` capability token, so the whole
        # recipe would fail instead of rendering untilted. Tell it to update.
        return {
            "code": "client_update_required",
            "feature": "clip_tilt",
            "minimum_client_protocol": settings.media_tilt_min_client_protocol,
        }
    if not clip_backdrop_media_client_supported() and recipe_has_clip_backdrop_media(recipe):
        # KRI-1139: a build below protocol 31 cannot decode a media backdrop (the
        # `clipBackdropMedia` token and the backdrop's `asset_id`), so the whole recipe would
        # fail instead of rendering the clip without its photo or video.
        return {
            "code": "client_update_required",
            "feature": "clip_backdrop_media",
            "minimum_client_protocol": settings.media_backdrop_min_client_protocol,
        }
    if not freeze_frame_client_supported() and recipe_has_freeze_frame(recipe):
        # KRI-828: the worker planned a creator's freeze (a main-track hold, capability
        # `freezeFrame`) that an older build cannot decode. Tell it to update.
        return {
            "code": "client_update_required",
            "feature": "freeze_frame",
            "minimum_client_protocol": settings.freeze_frame_min_client_protocol,
        }
    if not single_word_text_client_supported() and recipe_has_single_word_text(recipe):
        # One-word captions (`karaoke.single_word`): an older build cannot decode the key.
        return {
            "code": "client_update_required",
            "feature": "single_word_text",
            "minimum_client_protocol": settings.single_word_text_min_client_protocol,
        }
    manifest = getattr(recipe, "asset_manifest", None)
    if (
        manifest
        and any(isinstance(asset, ProcessedAudioRenderAsset) for asset in manifest.assets)
        and protocol < 18
    ):
        # Old clients reject this new discriminator while decoding the response.
        return {
            "code": "client_update_required",
            "feature": "processed_audio",
            "minimum_client_protocol": 18,
        }
    return None


def _client_update_required(update: dict, status: DeviceRenderStatus) -> HTTPException:
    """426 for a recipe this build cannot decode. It names the request (identity and
    phase), so the phone reports its decline for THIS request, never an older one it
    happens to hold a finished video for."""
    identity = status.request.identity
    named = (
        {"phase": status.phase, "identity": identity.model_dump(mode="json")}
        if isinstance(identity, DeviceRenderIdentity)
        else {}
    )
    return HTTPException(426, detail={**update, **named})


def _note_reader_protocol(job: Job, variant_id: str, record: dict) -> bool:
    """Remember the newest build that read this recipe without a 426. Returns whether
    the record changed (the caller commits)."""
    protocol = _caller_protocol()
    if protocol <= _reader_protocol(record):
        return False
    fresh = device_record(job, variant_id)
    fresh[READER_PROTOCOL_FIELD] = protocol
    save_device_record(job, variant_id, fresh)
    return True


def _carry_reader_protocol(job: Job, variant_id: str, previous: dict) -> None:
    """A re-pin of the same recipe keeps the newest reader: the device that could read it
    still holds it, so an older build's decline must not fail the fresh record either."""
    reader = max(_reader_protocol(previous), _caller_protocol())
    if reader <= 0:
        return
    fresh = device_record(job, variant_id)
    fresh[READER_PROTOCOL_FIELD] = max(reader, _reader_protocol(fresh))
    save_device_record(job, variant_id, fresh)


def _auto_resume_updated(
    job: Job, variant_id: str, record: dict, status: DeviceRenderStatus
) -> bool:
    """The phone said this build is too old for the recipe; the creator updated Kria.

    Re-deliver the same pinned, already-consented recipe under a fresh identity so the
    updated phone renders it on its own instead of showing "Update Kria" again. Only a
    build newer than the one that declined triggers this (the declining protocol is
    recorded with the report), and only one that decodes every protocol-gated feature of
    the recipe: the caller checks `_client_update_detail` first, so a build that is newer
    but still too old gets its 426 and the record stays as it is (no futile re-pin). And
    only a floor gap: whether a newer build renders a renderer/schema gap is something only
    the phone knows, so that record waits for its `renders_locally` retry instead.

    Bounded: a re-pin happens only for a caller newer than the recorded decliner, and a
    `renderer_outdated` decline is accepted only from a build at least as new as the
    newest reader (which survives the re-pin), so each re-pin needs a strictly newer build.
    """
    from app.services.client_protocol import current_client_protocol  # noqa: PLC0415

    if (
        current_client_protocol() is None
        or status.phase != "needs_attention"
        or status.reason_code != "renderer_outdated"
        or not isinstance(record.get(DECLINED_PROTOCOL_FIELD), int)
        or record.get(DECLINED_GAP_FIELD) != "floor"
        or not _caller_is_newer_than_decliner(record)
    ):
        return False
    try:
        retry_device_render(job, variant_id)
    except (CreatorRenderContractError, ValueError) as exc:
        if isinstance(exc, CreatorRenderContractError):
            record_contract_decline(job, variant_id, exc, stage="retry")
        return False
    fresh = device_record(job, variant_id)
    fresh["auto_resumed_at"] = datetime.now(UTC).isoformat()
    save_device_record(job, variant_id, fresh)
    _carry_reader_protocol(job, variant_id, record)
    apply_retry_variant_reset(job, variant_id)
    return True


def _auto_resume_reaped(
    job: Job, variant_id: str, record: dict, status: DeviceRenderStatus
) -> bool:
    """The reaper gave up on a silent phone; the creator just reopened the project.

    Re-deliver the same pinned, already-consented recipe under a fresh identity so
    the phone starts again on its own, instead of showing a 24h failure (S3-m).
    Only a phone build (it declares its protocol) triggers this, and only for a
    record the reaper itself failed.
    """
    from app.services.client_protocol import current_client_protocol  # noqa: PLC0415

    if (
        current_client_protocol() is None
        or status.phase != "needs_attention"
        or status.reason_code != "timed_out"
        or not record.get("reaped_at")
    ):
        return False
    try:
        retry_device_render(job, variant_id)
    except (CreatorRenderContractError, ValueError) as exc:
        if isinstance(exc, CreatorRenderContractError):
            record_contract_decline(job, variant_id, exc, stage="retry")
        return False
    fresh = device_record(job, variant_id)
    fresh["auto_resumed_at"] = datetime.now(UTC).isoformat()
    save_device_record(job, variant_id, fresh)
    _carry_reader_protocol(job, variant_id, record)
    apply_retry_variant_reset(job, variant_id)
    return True


async def _device_render_status(
    db: AsyncSession, user_id: uuid.UUID, job_id: uuid.UUID, variant_id: str
) -> DeviceRenderStatusOut:
    job = await _owned_job(db, user_id, job_id)
    try:
        status = device_status(job, variant_id)
    except KeyError as exc:
        raise HTTPException(404, "Device recipe unavailable") from exc
    record, status = await _settle_recorded_refusal(db, job, variant_id, status.request.identity)
    # The protocol gates run BEFORE any automatic re-pin: a build that still cannot decode
    # the recipe gets its 426 and re-pins nothing (a re-pin it would decline is futile).
    update = _client_update_detail(job, record, status)
    if update is None and (
        _auto_resume_reaped(job, variant_id, record, status)
        or _auto_resume_updated(job, variant_id, record, status)
    ):
        await db.commit()
        record, status = _record(job, device_status(job, variant_id).request.identity)
        update = _client_update_detail(job, record, status)
    noted = update is None and _note_reader_protocol(job, variant_id, record)
    if noted:
        # The note saved a fresh copy; the repair check below may save `record` again,
        # which must carry the reader (or an older build's decline would not be held).
        record, status = _record(job, status.request.identity)
    repair_before = copy.deepcopy(record.get("repair_authority"))
    if await _expire_repair_permission(db, job, record, status):
        record, status = _record(job, status.request.identity)
    renewed = record.get("repair_authority") != repair_before
    verification_required = "actual_export_policy" in record
    if update is not None and update.get("feature") == "export_verification":
        if noted:
            await db.commit()
        raise _client_update_required(update, status)
    if update is not None:
        raise _client_update_required(update, status)
    from app.services.device_render import device_export_maximum  # noqa: PLC0415

    response_fields = status.model_dump()
    maximum = device_export_maximum(job, variant_id)
    if maximum is not None:
        response_fields["request"]["max_export_duration_s"] = maximum
    from app.services.phone_destination import phone_rendering_allowed_for_job  # noqa: PLC0415

    # A rollout pause (cohort/flag off) holds a recipe the phone has not started:
    # asset grants and new reservations stay closed, and an export already reserved
    # still finalizes (runbook). The hold is a waiting state on the REAL phase, never
    # a synthetic needs_attention that `/retry` would then refuse (S3-b).
    rollout_paused = not phone_rendering_allowed_for_job(job) and status.phase == "awaiting_device"
    if rollout_paused:
        response_fields["reason"] = (
            "Rendering on iPhone is paused for now. Your edit is saved and will "
            "finish when it's back."
        )
    status = DeviceRenderStatusOut(
        **response_fields,
        phone_rendering_paused=rollout_paused,
        output_verification_required=verification_required,
        # Re-delivering an already-approved, already-pinned recipe needs no fresh
        # approval: `/device-render/retry` re-pins it directly, so no client is
        # ever routed to a chat approval that the newest turn would refuse (S3-n).
        retry_requires_approval=False,
        paused_by_user=device_paused(record),
    )
    now = datetime.now(UTC)
    last_polled_raw = record.get("last_polled_at")
    should_touch = True
    if isinstance(last_polled_raw, str):
        try:
            last_polled = datetime.fromisoformat(last_polled_raw)
        except ValueError:
            last_polled = None
        if last_polled is not None:
            should_touch = (now - last_polled) >= timedelta(seconds=_POLL_TOUCH_THROTTLE_S)
    if should_touch:
        touch_device_poll(job, variant_id, now)
    if should_touch or renewed or noted:
        await db.commit()
    return status


@router.post("/jobs/{job_id}/device-render/assets", response_model=DeviceAssetDownloadOut)
@limiter.limit("30/minute")
async def download_device_asset(
    request: Request,
    job_id: uuid.UUID,
    body: DeviceAssetDownloadBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DeviceAssetDownloadOut | JSONResponse:
    user_id = user.id
    job = await _owned_job(db, user_id, job_id)
    from app.services.phone_destination import phone_rendering_allowed_for_job  # noqa: PLC0415

    if not phone_rendering_allowed_for_job(job):
        # A rollout hold, not a device failure: the phone waits and asks again (S3-b).
        return _conflict_response(_rollout_paused())
    record, status = _record(job, body.identity)
    manifest = getattr(status.request.recipe, "asset_manifest", None)
    asset = next((a for a in manifest.assets if a.id == body.asset_id), None) if manifest else None
    if isinstance(asset, VisualRenderAsset):
        url = await _visual_download_url(db, job, user_id, asset)
        return DeviceAssetDownloadOut(
            asset_id=asset.id,
            download_url=url,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    if isinstance(asset, VoiceoverRenderAsset):
        url = await _voiceover_download_url(db, job, user_id, asset, record, body.identity)
        return DeviceAssetDownloadOut(
            asset_id=asset.id,
            download_url=url,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    if isinstance(asset, SongRenderAsset):
        url = await _song_download_url(db, job, user_id, asset)
        return DeviceAssetDownloadOut(
            asset_id=asset.id,
            download_url=url,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    if isinstance(asset, ProcessedAudioRenderAsset):
        url = await _processed_audio_download_url(db, job, asset, record, body.identity)
        return DeviceAssetDownloadOut(
            asset_id=asset.id,
            download_url=url,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    if not isinstance(asset, LibraryRenderAsset):
        raise HTTPException(404, "Library asset unavailable")
    try:
        path = await catalog_path(db, asset.catalog, asset.catalog_id)
        await db.rollback()
        actual = await asyncio.to_thread(
            inspect_library_asset,
            path,
            asset_id=asset.id,
            catalog=asset.catalog,
            catalog_id=asset.catalog_id,
        )
        if actual != asset:
            raise ValueError("library generation changed")
        job = await _owned_job(db, user_id, job_id)
        _record(job, body.identity)
        if await catalog_path(db, asset.catalog, asset.catalog_id) != path:
            raise ValueError("catalog changed")
        url = await asyncio.to_thread(
            storage.signed_get_url_for_generation,
            path,
            generation=asset.generation,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, "Library asset unavailable") from exc
    except ValueError as exc:
        raise HTTPException(409, "Library asset changed; refresh the recipe") from exc
    return DeviceAssetDownloadOut(
        asset_id=asset.id,
        download_url=url,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )


async def _visual_download_url(
    db: AsyncSession, job: Job, user_id: uuid.UUID, asset: VisualRenderAsset
) -> str:
    """Grant the job owner's own Visuals-pool photo or video, never another item's bytes.

    The recipe pinned one immutable generation; the device verifies the SHA-256.
    Here the row must still be this job's plan item's ready visual of the pinned
    kind and exact generation. Library-linked visuals retain their owned original
    source; unlinked uploads stay under the project's pool prefix. Removed or
    replaced sources fail closed instead of rendering different bytes.
    """
    try:
        visual_id = uuid.UUID(asset.visual_id)
    except ValueError as exc:
        raise HTTPException(404, "Visual unavailable") from exc
    row = await db.get(PlanItemAsset, visual_id, populate_existing=True)
    if (
        row is None
        or row.user_id != user_id
        or job.content_plan_item_id is None
        or row.plan_item_id != job.content_plan_item_id
    ):
        raise HTTPException(404, "Visual unavailable")
    if (
        row.status != "ready"
        or row.kind != asset.media_kind
        or str(row.gcs_generation or "") != asset.generation
    ):
        raise HTTPException(409, "Visual changed; refresh the recipe")
    if row.library_asset_id is not None or row.library_source_id is not None:
        source = (
            await db.get(MediaAssetSource, row.library_source_id, populate_existing=True)
            if row.library_source_id is not None
            else None
        )
        authorized = (
            source is not None
            and source.user_id == user_id
            and source.asset_id == row.library_asset_id
            and source.role == "original"
            and source.status == "available"
            and source.object_path == row.gcs_path
            and source.object_generation == asset.generation
            and source.uploaded_sha256 == asset.fingerprint.sha256
            and source.byte_count == asset.fingerprint.byte_count
        )
    else:
        prefix = f"users/{user_id}/plan/{row.plan_item_id}/pool/"
        authorized = row.gcs_path.startswith(prefix) and not any(
            part in {"", ".", ".."} for part in row.gcs_path[len(prefix) :].split("/")
        )
    if not authorized:
        raise HTTPException(409, "Visual changed; refresh the recipe")
    path = row.gcs_path
    await db.rollback()
    try:
        return await asyncio.to_thread(
            storage.signed_get_url_for_generation, path, generation=asset.generation
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(409, "Visual changed; refresh the recipe") from exc


def _approved_guided_narration(assembly: object) -> dict | None:
    guided = assembly.get("guided_edit") if isinstance(assembly, dict) else None
    proposal = guided.get("approved_proposal") if isinstance(guided, dict) else None
    narration = proposal.get("narration") if isinstance(proposal, dict) else None
    return narration if isinstance(narration, dict) else None


def _authorized_cleaned_narration(
    assembly: dict,
    user_id: uuid.UUID,
    item: object | None,
    asset: VoiceoverRenderAsset,
    record: dict,
) -> NarrationTrack | None:
    """The authorized cleaned narration for guided or narrated Jobs, else None.

    Returns None only when the Job requires no cleaned narration
    (the raw-voiceover grant applies unchanged). Otherwise every
    binding must still hold -- the item's current voiceover is the derivative's
    raw source, the pinned generation is the derivative's, the object sits
    under this owner's item/analysis prefix (`narration_matches_item`), and
    the Job's required_v1 preflight snapshot is the exact cut it records
    (`require_guided_cleanup_binding`) -- or this raises 409. Such a Job never
    falls back to granting the uncut recording.
    """
    changed = HTTPException(409, "Voiceover changed; refresh the recipe")
    try:
        if item is None:
            raise ValueError("item missing")
        narration = authorized_device_narration(
            record,
            assembly=assembly,
            owner_id=user_id,
            item=item,
            asset=asset,
        )
    except (ValueError, SpeechCleanupFailure) as exc:
        raise changed from exc
    return narration


def _device_speech_cleanup_outcome(
    assembly: dict, job_id: uuid.UUID, variant_id: str
) -> dict | None:
    """The public "Clean up speech" receipt for a device-rendered edit.

    The phone never reaches the cloud finalizer, so completion writes the same
    receipt the cloud finalizer would: ``applied`` when the published recipe
    played the cleaned derivative the Job's required_v1 snapshot consented to,
    ``checked_no_change``/``failed`` otherwise. None (nothing written) for
    every other Job.

    Two phone shapes carry this proof, checked in order:
      - A guided-story narration (`_approved_guided_narration`): re-binds the
        approved `NarrationTrack` to the snapshot via
        `require_guided_cleanup_binding` (a Job pinned before guided cleanup
        existed carries no cleaned narration, so this yields ``None`` rather
        than a false "failed" receipt for a native re-export of it).
      - A subtitled/self-narrated (`_run_phone_subtitled_job`) or
        narrated-with-voiceover (`_run_phone_narrated_job`) render: both
        persist their OWN `_speech_cleanup_outcome_context` directly on the
        completed ``variant_id`` entry at plan time (mirrors the cloud
        renderer's `base["_speech_cleanup_outcome_context"]`) -- no separate
        re-binding proof is needed here, since that variant was only ever
        pinned by the SAME render pass that validated the snapshot.
    """
    narration = _approved_guided_narration(assembly)
    if narration is not None:
        if assembly.get("speech_cleanup_contract") != "required_v1":
            return None
        try:
            if narration_speech_cleanup(narration) is None:
                return None
            context = require_guided_cleanup_binding(assembly, narration)
        except (ValueError, SpeechCleanupFailure):
            context = None
        return build_preflight_public_outcome(
            assembly,
            job_id=str(job_id),
            results=[{"ok": True, "_speech_cleanup_outcome_context": context}],
        )
    variant = next(
        (v for v in assembly.get("variants") or [] if v.get("variant_id") == variant_id), None
    )
    if not isinstance(variant, dict):
        return None
    context = variant.get("_speech_cleanup_outcome_context")
    if context is None:
        return None
    return build_preflight_public_outcome(
        assembly,
        job_id=str(job_id),
        results=[{"ok": True, "_speech_cleanup_outcome_context": context}],
    )


async def _voiceover_download_url(
    db: AsyncSession,
    job: Job,
    user_id: uuid.UUID,
    asset: VoiceoverRenderAsset,
    record: dict,
    identity: DeviceRenderIdentity,
) -> str:
    """Grant the job owner's own plan-item voiceover, never another item's bytes.

    Unlike a Visuals-pool asset (one row per photo/video, looked up by its
    own id), a voiceover is a bare `PlanItem` column -- ownership is already
    established by `_owned_job`'s lock chain (job -> item -> plan ->
    `plan.user_id == user_id`), so this looks the item up by the JOB's own
    `content_plan_item_id`, never by parsing the asset's `plan_item_id`
    (only cross-checked here, defense-in-depth, against the job's already-
    trusted value). The recipe pinned one immutable generation; the device
    re-hashes the downloaded bytes against the pinned SHA-256. The item must
    still be in "voiceover" audio mode with that exact `(path, generation)`
    attached, so a cleared or replaced voiceover fails closed instead of
    granting different bytes. A "Clean up speech" guided Job grants its cleaned
    derivative instead (`_authorized_cleaned_narration`), never the raw recording.
    """
    if job.content_plan_item_id is None or str(job.content_plan_item_id) != asset.plan_item_id:
        raise HTTPException(404, "Voiceover unavailable")
    item = await db.get(PlanItem, job.content_plan_item_id, populate_existing=True)
    item_snapshot = (
        SimpleNamespace(
            id=item.id,
            audio_mode=item.audio_mode,
            voiceover_gcs_path=item.voiceover_gcs_path,
            voiceover_generation=item.voiceover_generation,
            voiceover_duration_s=getattr(item, "voiceover_duration_s", None),
        )
        if item is not None
        else None
    )
    assembly_snapshot = copy.deepcopy(job.assembly_plan or {})
    job_id_value = job.id
    # Legacy recovery streams a derivative to verify its manifest fingerprint.
    # Release the request transaction before that potentially slow storage I/O.
    await db.rollback()
    try:
        narration_snapshot = await asyncio.to_thread(
            _authorized_cleaned_narration, assembly_snapshot, user_id, item_snapshot, asset, record
        )
    except (FileNotFoundError, NotFound) as exc:
        raise HTTPException(409, "Voiceover changed; refresh the recipe") from exc
    path = narration_snapshot.gcs_path if narration_snapshot is not None else None
    # Re-establish the identity/owner fence after external I/O before granting.
    job = await _owned_job(db, user_id, job_id_value)
    current_record, _ = _record(job, identity)
    if current_record.get("status", {}).get("request") != record.get("status", {}).get(
        "request"
    ) or current_record.get("narration_binding") != record.get("narration_binding"):
        raise HTTPException(409, "Device recipe changed")
    current_item = await db.get(PlanItem, job.content_plan_item_id, populate_existing=True)
    if path is None:
        if (
            current_item is None
            or getattr(current_item, "audio_mode", None) != "voiceover"
            or not current_item.voiceover_gcs_path
            or str(current_item.voiceover_generation or "") != asset.generation
        ):
            raise HTTPException(409, "Voiceover changed; refresh the recipe")
        path = str(current_item.voiceover_gcs_path)
    else:
        try:
            if current_record.get("narration_binding") is not None:
                narration = authorized_device_narration(
                    current_record,
                    assembly=job.assembly_plan or {},
                    owner_id=user_id,
                    item=current_item,
                    asset=asset,
                )
            else:
                narration = narration_snapshot
                require_guided_cleanup_binding(job.assembly_plan or {}, narration)
        except (ValueError, SpeechCleanupFailure) as exc:
            raise HTTPException(409, "Voiceover changed; refresh the recipe") from exc
        if (
            narration is None
            or narration.gcs_path != path
            or not narration_matches_item(
                narration.model_dump(mode="json"), current_item, owner_id=user_id
            )
        ):
            raise HTTPException(409, "Voiceover changed; refresh the recipe")
    await db.rollback()
    try:
        return await asyncio.to_thread(
            storage.signed_get_url_for_generation, path, generation=asset.generation
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(409, "Voiceover changed; refresh the recipe") from exc


async def _song_download_url(
    db: AsyncSession,
    job: Job,
    user_id: uuid.UUID,
    asset: SongRenderAsset,
) -> str:
    """Grant the job owner's own plan-item song (KRI-374), never another item's bytes.

    Cloned from ``_voiceover_download_url`` minus the cleaned-narration branch: a
    song is a bare ``PlanItem`` column, so ownership is already established by
    ``_owned_job``'s lock chain and the item is looked up by the JOB's own
    ``content_plan_item_id`` (the asset's ``plan_item_id`` is only cross-checked
    against it: another item => 404). The item must still be in ``song`` audio mode
    with the recipe's exact generation attached, so a removed or replaced song fails
    closed (409) instead of granting different bytes. The device re-hashes the
    download against the pinned SHA-256.
    """
    if job.content_plan_item_id is None or str(job.content_plan_item_id) != asset.plan_item_id:
        raise HTTPException(404, "Song unavailable")
    item = await db.get(PlanItem, job.content_plan_item_id, populate_existing=True)
    if (
        item is None
        or getattr(item, "audio_mode", None) != "song"
        or not getattr(item, "song_gcs_path", None)
        or str(getattr(item, "song_generation", None) or "") != asset.generation
    ):
        raise HTTPException(409, "Song changed; refresh the recipe")
    path = str(item.song_gcs_path)
    await db.rollback()
    try:
        return await asyncio.to_thread(
            storage.signed_get_url_for_generation, path, generation=asset.generation
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(409, "Song changed; refresh the recipe") from exc


async def _processed_audio_download_url(
    db: AsyncSession,
    job: Job,
    asset: ProcessedAudioRenderAsset,
    record: dict,
    identity: DeviceRenderIdentity,
) -> str:
    """Grant only the exact job/variant/source derivative pinned in the recipe.

    The object path is read exclusively from private worker state.  Neither the
    client nor the portable manifest supplies storage authority.
    """
    from app.pipeline.phone_processed_audio import (  # noqa: PLC0415
        processed_audio_asset,
        processed_audio_receipt_for,
    )

    changed = HTTPException(409, "Processed audio changed; refresh the recipe")
    if (
        asset.job_id != str(job.id)
        or asset.variant_id != identity.variant_id
        or asset.render_generation != str(record.get("base_generation") or "")
    ):
        raise changed
    receipt = processed_audio_receipt_for(
        job.assembly_plan or {}, variant_id=asset.variant_id, source_media_id=asset.source_media_id
    )
    if receipt is None or processed_audio_asset(receipt, asset_id=asset.id) != asset:
        raise changed
    try:
        sources = approved_export_sources(job, record["status"]["request"])
    except (KeyError, CreatorRenderContractError) as exc:
        raise changed from exc
    source = sources.get(asset.source_media_id)
    if source is None or source.generation != asset.source_generation:
        raise changed
    prefix = (
        f"generative-jobs/{job.id}/phone-processed-audio/"
        f"{asset.variant_id}_{asset.source_media_id}_"
    )
    token = receipt.path[len(prefix) : -len(".m4a")] if receipt.path.endswith(".m4a") else ""
    if (
        not receipt.path.startswith(prefix)
        or len(token) not in {32, 64}
        or any(char not in "0123456789abcdef" for char in token)
    ):
        raise changed
    await db.rollback()
    try:
        return await asyncio.to_thread(
            storage.signed_get_url_for_generation, receipt.path, generation=asset.generation
        )
    except (FileNotFoundError, ValueError) as exc:
        raise changed from exc


@router.post("/jobs/{job_id}/device-render/uploads", response_model=DeviceExportReservationOut)
@limiter.limit("10/minute")
async def reserve_device_export(
    request: Request,
    job_id: uuid.UUID,
    body: DeviceExportReservationBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DeviceExportReservationOut | JSONResponse:
    job = await _owned_job(db, user.id, job_id)
    from app.services.phone_destination import phone_rendering_allowed_for_job  # noqa: PLC0415

    record, status = _record(job, body.identity)
    already_reserved = status.phase == "syncing" and str(body.attempt_id) in record["attempts"]
    if not phone_rendering_allowed_for_job(job) and not already_reserved:
        # A rollout hold keeps new reservations closed; an export already reserved
        # still finalizes (runbook). The phone waits instead of failing (S3-b).
        return _conflict_response(_rollout_paused())
    if status.phase not in {"awaiting_device", "syncing"}:
        raise HTTPException(409, "Render no longer accepts uploads")
    # Uploading means the phone resumed: a user Stop no longer holds this record.
    record.pop(PAUSED_FIELD, None)
    attempt = str(body.attempt_id)
    attempts = record["attempts"]
    previous = attempts.get(attempt)
    if previous and (
        previous["size"] != body.file_size_bytes
        or previous["sha256"] != body.sha256
        # A different tail is different bytes, even at the same length.
        or previous.get("brand_tail", "none") != body.brand_tail
    ):
        raise HTTPException(409, "Upload attempt reused for different bytes")
    if not previous and len(attempts) >= 5:
        raise HTTPException(409, "Too many export attempts for this recipe")
    path = f"{user.id}/{job.id}/device/{body.attempt_id}.mp4"
    now = datetime.now(UTC)
    cleanup = (
        await db.execute(
            select(TemporaryMediaUpload)
            .where(TemporaryMediaUpload.object_path == path)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cleanup is None:
        cleanup = TemporaryMediaUpload(
            user_id=user.id,
            object_path=path,
            purpose="device_export",
            status="reserved",
            retention_expires_at=now + timedelta(hours=24),
        )
        db.add(cleanup)
    elif (
        cleanup.user_id != user.id
        or cleanup.status != "reserved"
        or cleanup.retention_expires_at <= now
    ):
        raise HTTPException(409, "Upload reservation expired")
    attempts[attempt] = {
        "path": path,
        "size": body.file_size_bytes,
        "sha256": body.sha256,
        "brand_tail": body.brand_tail,
    }
    status.phase = "syncing"
    record["status"] = status.model_dump(mode="json")
    save_device_record(job, body.identity.variant_id, record)
    await db.commit()
    url = await asyncio.to_thread(storage.signed_put_url, path, "video/mp4", body.file_size_bytes)
    return DeviceExportReservationOut(
        attempt_id=body.attempt_id,
        upload_url=url,
        upload_headers={"Content-Type": "video/mp4", "x-goog-if-generation-match": "0"},
        expires_at=now + timedelta(minutes=15),
    )


@router.post("/jobs/{job_id}/device-render/failures", response_model=DeviceRenderFailureOut)
@limiter.limit("10/minute")
async def report_device_failure(
    request: Request,
    job_id: uuid.UUID,
    body: DeviceRenderFailureBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DeviceRenderFailureOut | JSONResponse:
    """The phone could not produce (or finish uploading) a local export.

    Without this endpoint a phone-side crash/cancel/thermal-throttle leaves
    the job parked `awaiting_device`/`syncing` forever — nothing else ever
    tells the server the device gave up. The recipe stays pinned (never
    `processing_failed`): `/device-render/retry` re-pins the same recipe
    under a fresh identity once the user asks to try again.

    A user Stop (`cancelled_by_user`) is a resumable pause, not a failure: the
    record keeps its phase, the variant and Job keep their state, and no failure
    card is posted. A failure on an edit whose variant already has an accepted
    output never relabels that variant or the Job (the last good video stays).
    """
    try:
        job = await _owned_job(db, user.id, job_id)
        record, status = _record(job, body.identity)
    except _DeviceConflict as conflict:
        return _conflict_response(conflict)
    if status.phase == "published":
        return _conflict_response(_DeviceConflict("published", "Render already published"))
    if status.phase == "needs_attention":
        # Idempotent: a duplicate/retried failure report for the same pinned
        # identity returns the current state rather than erroring.
        return DeviceRenderFailureOut(
            identity=body.identity,
            phase="needs_attention",
            reason_code=status.reason_code or body.reason_code,
        )
    if body.reason_code == "cancelled_by_user":
        mark_device_paused(job, body.identity.variant_id, datetime.now(UTC))
        await db.commit()
        return DeviceRenderFailureOut(
            identity=body.identity, phase="paused", reason_code="cancelled_by_user"
        )
    from app.services.phone_destination import phone_rendering_allowed_for_job  # noqa: PLC0415

    if not phone_rendering_allowed_for_job(job):
        # Phone rendering is rolled back for this account. A failure the phone hit
        # under the hold (an asset grant or reservation it was refused, from a build
        # that cannot tell) is the rollout, not the device: keep the record waiting,
        # leave the variant and Job alone, post no card (S3-b, rollback runbook).
        return DeviceRenderFailureOut(
            identity=body.identity, phase="paused", reason_code=body.reason_code
        )
    if body.reason_code == "renderer_outdated" and (
        status.phase == "syncing" or _caller_protocol() < _reader_protocol(record)
    ):
        # Two devices: a newer build read this recipe (or one is uploading its video). An
        # older build being too old is no reason to fail the record that device holds;
        # failing it would only make the newer device re-pin it, and the old one decline
        # it again, forever. The old phone keeps its own "Update Kria".
        return _conflict_response(
            _DeviceConflict(
                "held_by_newer_build",
                "Another device with a newer version of Kria is rendering this edit.",
            )
        )
    try:
        mark_device_failed(
            job, body.identity.variant_id, reason_code=body.reason_code, detail=body.detail
        )
    except ValueError as exc:
        raise HTTPException(409, "Render no longer accepts a failure report") from exc
    if body.reason_code == "renderer_outdated":
        # Remember which build declined, so `/retry` from it answers requires_update and
        # a newer build re-pins on its own (`_auto_resume_updated`).
        failed = device_record(job, body.identity.variant_id)
        failed[DECLINED_PROTOCOL_FIELD] = _caller_protocol()
        failed[DECLINED_GAP_FIELD] = (
            "floor" if _client_update_detail(job, record, status) is not None else "renderer"
        )
        save_device_record(job, body.identity.variant_id, failed)
    if not has_accepted_artifact(job, body.identity.variant_id):
        apply_device_failure_variant_update(
            job, body.identity.variant_id, reason_code=body.reason_code, detail=body.detail
        )
    await record_native_activity(db, job, body.identity.variant_id)
    await db.commit()
    await asyncio.to_thread(_nudge_kria_observer, job_id)
    return DeviceRenderFailureOut(
        identity=body.identity, phase="needs_attention", reason_code=body.reason_code
    )


@router.post("/jobs/{job_id}/device-render/retry", response_model=DeviceRetryOut)
@limiter.limit("5/minute")
async def retry_device_export(
    request: Request,
    job_id: uuid.UUID,
    body: DeviceRetryBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DeviceRetryOut | JSONResponse:
    """Re-pin the SAME approved recipe under a fresh (revision + 1) identity.

    This is a pure re-delivery vehicle — it never re-runs decision logic
    (regenerate is a separate, existing product action). The phone sees a
    new identity, re-downloads assets, and re-exports from scratch.

    Re-delivery needs no fresh approval, also for a chat-approved (runtime-v2)
    program: the recipe, its generation and its prepared-attempt fence are the
    ones the creator already approved, and the pin re-verifies them. A paused
    record resumes under its current identity.
    """
    try:
        return await _retry_device_export(
            db, user.id, job_id, body.identity, renders_locally=body.renders_locally
        )
    except _DeviceConflict as conflict:
        return _conflict_response(conflict)


async def _retry_device_export(
    db: AsyncSession,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    identity: DeviceRenderIdentity,
    *,
    renders_locally: bool = False,
) -> DeviceRetryOut:
    job = await _owned_job(db, user_id, job_id)
    try:
        record, status = _record(job, identity)
    except _ContractRefusal as refusal:
        await _refuse_publication(db, job, refusal, stage="retry")
        raise
    if status.phase != "needs_attention":
        if clear_device_pause(job, identity.variant_id) or device_paused(record):
            # Resume after a user Stop: the same identity is still pinned and live.
            await db.commit()
            return DeviceRetryOut(identity=status.request.identity, phase="awaiting_device")
        raise _DeviceConflict("not_awaiting_retry", "Render is not awaiting a retry")
    decline = record.get(CONTRACT_DECLINE_FIELD)
    if device_contract_blocks_retry(record) or status.reason_code == "unsupported_recipe":
        # The same recipe would be refused again: say why instead of looping (S3-o).
        message = (
            decline.get("message") if isinstance(decline, dict) else None
        ) or "This edit needs a change before it can render again. Your edit is saved."
        raise _DeviceConflict("requires_change", message)
    if (
        status.reason_code == "renderer_outdated"
        and not renders_locally
        and not _caller_is_newer_than_decliner(record)
    ):
        # The build that declined this recipe would decline the re-pin the same way:
        # the way forward is updating Kria, never another retry (no loop). A phone that
        # checked the pinned recipe against its own renderer and can render it now (an
        # operator lowered a protocol floor) says so, and its retry goes through: the
        # "Try again" it shows then works instead of flipping to "Update Kria".
        raise _DeviceConflict(
            "requires_update",
            "This edit needs a newer version of Kria. Update the app to finish it. "
            "Your edit is saved.",
        )
    try:
        new_status = retry_device_render(job, identity.variant_id)
    except CreatorRenderContractError as exc:
        # The same recipe can no longer be re-pinned: say so (typed), keep the old
        # state, and never replace the last accepted artifact.
        record_contract_decline(job, identity.variant_id, exc, stage="retry")
        await db.commit()
        raise _DeviceConflict("requires_change", str(exc)) from exc
    except ValueError as exc:
        raise _DeviceConflict("not_awaiting_retry", str(exc)) from exc
    _carry_reader_protocol(job, identity.variant_id, record)
    apply_retry_variant_reset(job, identity.variant_id)
    await db.commit()
    return DeviceRetryOut(identity=new_status.request.identity, phase="awaiting_device")


class _ExportIntegrityError(ValueError):
    """The stored bytes are not the reserved upload: the only file defect that withholds."""


class _ExportReadError(RuntimeError):
    """Reserved bytes could not be read, so publication must wait."""


def _download_reserved_export(
    path: str, local: Path, generation: str, expected_size: int, expected_sha256: str
) -> None:
    try:
        storage.download_generation_to_file(path, str(local), generation=generation)
        size = local.stat().st_size
        with local.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
    except Exception as exc:
        raise _ExportReadError("export bytes unavailable") from exc
    if size != expected_size:
        raise _ExportIntegrityError("export size mismatch")
    if digest != expected_sha256:
        raise _ExportIntegrityError("export checksum mismatch")


def _verify_export(
    path: str,
    generation: str,
    expected_size: int,
    expected_sha256: str,
    status: DeviceRenderStatus,
    brand_tail: str = "none",
    *,
    poster_job_id: uuid.UUID | None = None,
    source_bindings: dict | None = None,
    verification_report: dict | None = None,
    export_measurements: dict | None = None,
    max_export_duration_s: float | None = None,
    advisories: list[dict] | None = None,
) -> str | None:
    """Measure the phone's export; with ``poster_job_id``, also publish its poster.

    Returns the poster key, or ``None`` when no poster was requested or the
    extraction failed. The poster is fail-open: a healthy export still
    publishes without one, and the library's repair path can mint it later.
    Only a byte mismatch (`_ExportIntegrityError`) raises. A quality finding never
    withholds the video: the duration cap lands in ``advisories`` and the picture,
    audio and text verdicts in ``verification_report``, whatever they say.
    """
    with tempfile.TemporaryDirectory(prefix="kria_device_export_") as directory:
        local = Path(directory) / "export.mp4"
        _download_reserved_export(path, local, generation, expected_size, expected_sha256)
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-f",
                "mov",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(local),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        probe = json.loads(result.stdout)
        video = [s for s in probe["streams"] if s["codec_type"] == "video"]
        audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
        recipe = status.request.recipe
        if (
            len(video) != 1
            or video[0].get("codec_name") != "h264"
            or video[0].get("width") != recipe.canvas.width
            or video[0].get("height") != recipe.canvas.height
        ):
            raise ValueError("export video format mismatch")
        stream = video[0]
        try:
            actual_fps = float(Fraction(str(stream.get("avg_frame_rate", "0/1"))))
            rotation = float((stream.get("tags") or {}).get("rotate", 0))
            rotations = [
                float(entry["rotation"])
                for entry in stream.get("side_data_list", [])
                if "rotation" in entry
            ] + [rotation]
        except (ValueError, ZeroDivisionError, TypeError) as exc:
            raise ValueError("export video format mismatch") from exc
        if (
            stream.get("pix_fmt") != "yuv420p"
            or abs(actual_fps - recipe.frame_rate) > 0.01
            or any(not math.isfinite(value) or value % 360 != 0 for value in rotations)
        ):
            raise ValueError("export video format mismatch")
        requires_audio = (
            recipe.schema_version == 2
            or recipe.audio.music_asset_id is not None
            or any(track.kind == "audio" and track.clips for track in recipe.tracks)
        )
        if (
            (requires_audio and not audio)
            or len(audio) > 1
            or any(s.get("codec_name") != "aac" for s in audio)
        ):
            raise ValueError("export audio format mismatch")
        duration = float(probe["format"]["duration"])
        # The phone appends brand furniture after the edit, so the file is
        # legitimately longer than the recipe. It declared which tail it used at
        # reservation time and the length of that tail is ours, not its.
        expected = recipe.duration + BRAND_TAIL_SECONDS[brand_tail]
        if not math.isfinite(duration) or abs(duration - expected) > max(
            0.1, 2 / recipe.frame_rate
        ):
            raise ValueError("export duration mismatch")
        maximum = max_export_duration_s
        if maximum is not None:
            from app.services.export_duration_policy import check_export_maximum  # noqa: PLC0415

            try:
                check_export_maximum(duration, maximum)
            except CreatorRenderContractError:
                if advisories is not None:
                    advisories.append(
                        {
                            "kind": "duration_cap",
                            "status": "failed",
                            "reason": "over_maximum",
                            "actual_s": round(duration, 3),
                            "expected_s": maximum,
                        }
                    )
        if export_measurements is not None:
            export_measurements["measured_export_duration_s"] = duration
        if verification_report is not None:
            from app.services.actual_export_verification import (
                verify_actual_export,  # noqa: PLC0415
            )

            sources = {}
            total_size = 0
            try:
                for index, (media_id, binding) in enumerate((source_bindings or {}).items()):
                    target = Path(directory) / f"reference-{index}.mp4"
                    storage.download_generation_to_file(
                        binding.proxy_path, str(target), generation=binding.generation
                    )
                    total_size += target.stat().st_size
                    if total_size > 128 * 1024 * 1024:
                        raise ValueError("reference download budget")
                    sources[media_id] = target
                verification_report.update(
                    verify_actual_export(local, status, sources, brand_tail=brand_tail)
                )
            except Exception as exc:  # noqa: BLE001 - uncertainty is recorded, never blocks
                verification_report.update(
                    status="partial",
                    checks=[
                        {
                            "kind": "evidence",
                            "status": "missing",
                            "reason": type(exc).__name__,
                        }
                    ],
                )
        if poster_job_id is None:
            return None
        # Reuse the verified download: phone renders never pass through a
        # worker, so this is the only point that holds the published bytes.
        return _export_poster(local, path, poster_job_id)


def _export_poster(local: Path, path: str, job_id: uuid.UUID) -> str | None:
    try:
        return upload_video_poster(str(local), path, job_id=job_id)
    except Exception as exc:  # noqa: BLE001 - poster is fail-open by contract
        log.warning(
            "device_export_poster_failed",
            job_id=str(job_id),
            video_path=path,
            error_class=type(exc).__name__,
            error=str(exc)[:300],
        )
        return None


def _poster_of_unmeasured_export(
    path: str, generation: str, job_id: uuid.UUID, expected_size: int, expected_sha256: str
) -> str | None:
    """Check reserved bytes even when quality measurements are unavailable."""
    with tempfile.TemporaryDirectory(prefix="kria_device_poster_") as directory:
        local = Path(directory) / "export.mp4"
        _download_reserved_export(path, local, generation, expected_size, expected_sha256)
        return _export_poster(local, path, job_id)


@router.post("/jobs/{job_id}/device-render/complete", response_model=DeviceExportCompleteOut)
@limiter.limit("5/minute")
async def complete_device_export(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    job_id: uuid.UUID,
    body: DeviceExportCompleteBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DeviceExportCompleteOut:
    result = await _complete_device_export(job_id, body, user.id, db)
    if result.status == "checking":
        response.status_code = 202
        from app.tasks.export_verification import enqueue  # noqa: PLC0415

        background_tasks.add_task(enqueue, str(job_id), str(user.id), body.model_dump(mode="json"))
    return result


async def _complete_device_export(
    job_id: uuid.UUID,
    body: DeviceExportCompleteBody,
    user_id: uuid.UUID,
    db: AsyncSession,
    *,
    verification_token: str | None = None,
    unverified: bool = False,
) -> DeviceExportCompleteOut:
    """``unverified``: the output checks could not run for the whole recheck window, so the
    finished export publishes without them (bytes still match the reserved upload)."""
    job = await _owned_job(db, user_id, job_id)
    try:
        record, status = _record(job, body.identity, contract_advisory=[])
    except _ContractRefusal as refusal:
        await _refuse_publication(db, job, refusal)
        raise
    if await _expire_repair_permission(db, job, record, status):
        raise HTTPException(409, "Automatic repair permission expired")
    attempt_id = str(body.attempt_id)
    if status.phase == "published":
        if record.get("published_attempt") != attempt_id:
            raise HTTPException(409, "Another export was published")
        return DeviceExportCompleteOut(identity=body.identity)
    if status.phase != "syncing":
        raise HTTPException(409, "Render no longer accepts completion")
    attempt = record["attempts"].get(attempt_id)
    if attempt is None:
        raise HTTPException(409, "Export was not reserved")
    if "actual_export_policy" in record:
        from app.tasks.export_verification import claim_valid  # noqa: PLC0415

        if verification_token is None:
            from app.services.client_protocol import current_client_protocol  # noqa: PLC0415

            if (current_client_protocol() or 0) < 13:
                raise HTTPException(
                    426,
                    detail={"code": "client_update_required", "feature": "background_verification"},
                )
            active = record.get("verification_attempt")
            if active and active != attempt_id:
                raise HTTPException(409, "Another export is being checked")
            if "verification_task" not in attempt:
                attempt["verification_task"] = {
                    "state": "queued",
                    "runs": 0,
                    "queued_at": datetime.now(UTC).isoformat(),
                }
                record["verification_attempt"] = attempt_id
                save_device_record(job, body.identity.variant_id, record)
                from app.kria.automatic_export_repair import bind_repair_grant  # noqa: PLC0415

                await bind_repair_grant(db, job, body.identity.variant_id)
                await db.commit()
            return DeviceExportCompleteOut(status="checking", identity=body.identity)
        if record.get("verification_attempt") != attempt_id or not claim_valid(
            record, attempt_id, verification_token
        ):
            raise HTTPException(409, "Verification claim changed")
    verification_report: dict | None = {} if "actual_export_policy" in record else None
    policy = record.get("actual_export_policy")
    bindings = (
        approved_export_sources(job, status.request) if verification_report is not None else None
    )
    sources_digest = export_source_digest(bindings) if bindings is not None else None
    if verification_report is not None:
        supported_policy = isinstance(policy, dict) and policy.get("version") == 1
        if not supported_policy or policy.get("source_receipts_digest") != sources_digest:
            verification_report.update(
                status="rejected" if supported_policy else "unsupported",
                checks=[{"kind": "authority", "status": "failed"}],
            )
            record["attempts"][attempt_id]["actual_export_verification"] = verification_report
            save_device_record(job, body.identity.variant_id, record)
            cause = CreatorRenderContractError(
                "I couldn't confirm this export belongs to your approved edit.",
                decline_reason="evidence_missing" if supported_policy else "capability_unavailable",
                field_path="source_identity" if supported_policy else "render_verifier",
                alternative="Review and approve a new edit before exporting again.",
            )
            await _refuse_publication(db, job, _ContractRefusal(cause, body.identity.variant_id))
            raise HTTPException(422, "Export verification authority changed")
    verify_kwargs = (
        {"source_bindings": bindings, "verification_report": verification_report}
        if verification_report is not None
        else {}
    )
    export_measurements: dict = {}
    from app.services.device_render import device_export_maximum  # noqa: PLC0415

    maximum = device_export_maximum(job, body.identity.variant_id)
    if maximum is not None:
        verify_kwargs["export_measurements"] = export_measurements
        verify_kwargs["max_export_duration_s"] = maximum
    # Quality findings are advisory: they are recorded and the video still publishes.
    # Only bytes that are not the reserved upload are withheld.
    advisories: list[dict] = []
    integrity_failure: _ExportIntegrityError | None = None
    # Do not hold database locks while downloading/probing the MP4. Reacquire the full fence below.
    await db.rollback()
    metadata = None
    poster_path = None
    try:
        metadata = await asyncio.to_thread(storage.object_metadata, attempt["path"])
        if (
            metadata.size != attempt["size"]
            or metadata.content_type != "video/mp4"
            or not metadata.generation
        ):
            raise _ExportIntegrityError("export metadata mismatch")
        if unverified and verification_report is not None:
            advisories.append(
                {
                    "kind": "verification",
                    "status": "unavailable",
                    "reason": "checks_could_not_run",
                }
            )
            verification_report["status"] = "partial"
            poster_path = await asyncio.to_thread(
                _poster_of_unmeasured_export,
                attempt["path"],
                str(metadata.generation),
                job_id,
                attempt["size"],
                attempt["sha256"],
            )
        else:
            poster_path = await asyncio.to_thread(
                _verify_export,
                attempt["path"],
                str(metadata.generation),
                attempt["size"],
                attempt["sha256"],
                status,
                attempt.get("brand_tail", "none"),
                poster_job_id=job_id,
                advisories=advisories,
                **verify_kwargs,
            )
    except (FileNotFoundError, _ExportReadError) as exc:
        raise HTTPException(409, "Export upload bytes are unavailable") from exc
    except _ExportIntegrityError as exc:
        integrity_failure = exc
    except (ValueError, KeyError, subprocess.SubprocessError) as exc:
        if metadata is None:
            integrity_failure = _ExportIntegrityError("export metadata unavailable")
        else:
            # The file could not be read as the edit's video (format, codec, probe).
            advisories.append(
                {
                    "kind": "format",
                    "status": "failed",
                    "reason": re.sub(r"\W+", "_", str(exc).lower()).strip("_")[:80]
                    if isinstance(exc, ValueError)
                    else type(exc).__name__,
                }
            )
            try:
                poster_path = await asyncio.to_thread(
                    _poster_of_unmeasured_export,
                    attempt["path"],
                    str(metadata.generation),
                    job_id,
                    attempt["size"],
                    attempt["sha256"],
                )
            except _ExportReadError as read_error:
                raise HTTPException(409, "Export upload bytes are unavailable") from read_error
            except _ExportIntegrityError as byte_error:
                integrity_failure = byte_error
    if integrity_failure is not None:
        if verification_report is not None:
            verification_report.update(
                status="rejected", checks=[{"kind": "format", "status": "failed"}]
            )
    elif verification_report is not None and advisories:
        verification_report.setdefault("checks", []).extend(advisories)
        if verification_report.get("status") in (None, "checked"):
            verification_report["status"] = "rejected"
    # A short-lived (5-min) default TTL here would expire long before playback:
    # match the playback TTL every other read path re-signs against (see
    # PLAYBACK_URL_TTL_MIN docstring in generative_jobs.py).
    url = (
        await asyncio.to_thread(storage.signed_get_url, attempt["path"], PLAYBACK_URL_TTL_MIN)
        if integrity_failure is None
        else None
    )
    job = await _owned_job(db, user_id, job_id)
    contract_changes: list[CreatorRenderContractError] = []
    try:
        record, status = _record(job, body.identity, contract_advisory=contract_changes)
    except _ContractRefusal as refusal:
        await _refuse_publication(db, job, refusal)
        raise
    if status.phase == "published":
        if record.get("published_attempt") != attempt_id:
            raise HTTPException(409, "Another export was published")
        return DeviceExportCompleteOut(identity=body.identity)
    if status.phase != "syncing":
        raise HTTPException(409, "Render no longer accepts completion")
    if record.get("actual_export_policy") != policy:
        raise HTTPException(409, "Export verification authority changed")
    current_attempt = record["attempts"].get(attempt_id)
    if current_attempt is None or any(
        current_attempt.get(key) != attempt.get(key)
        for key in ("path", "size", "sha256", "brand_tail")
    ):
        raise HTTPException(409, "Upload reservation changed")
    if verification_token is not None:
        if await _expire_repair_permission(db, job, record, status):
            raise HTTPException(409, "Automatic repair permission expired")
        from app.tasks.export_verification import claim_valid  # noqa: PLC0415

        if not claim_valid(record, attempt_id, verification_token):
            raise HTTPException(409, "Verification claim changed")
        record["attempts"][attempt_id]["verification_task"].update(state="done")
    if integrity_failure is not None and verification_report is None:
        raise HTTPException(422, "Export does not match the approved recipe")
    quality_warning: dict | None = (
        {"status": "rejected", "failed_checks": sorted({a["kind"] for a in advisories})}
        if advisories and verification_report is None
        else None
    )
    if verification_report is not None:
        # Fence the source/policy snapshot again after downloading and decoding.
        if (
            record.get("actual_export_policy") != policy
            or export_source_digest(approved_export_sources(job, status.request)) != sources_digest
        ):
            raise HTTPException(409, "Export verification authority changed")
        verification_report.update(
            artifact_path=attempt["path"],
            storage_generation=str(metadata.generation) if metadata is not None else None,
            source_receipts_digest=sources_digest,
        )
        record["attempts"][attempt_id]["actual_export_verification"] = verification_report
        if record.get("repair_history"):
            row = record["repair_history"][-1]
            row["outcome"] = verification_report.get("status", "partial")
            row["completed_at"] = datetime.now(UTC).isoformat()
            row["elapsed_s"] = (
                datetime.now(UTC) - datetime.fromisoformat(row["started_at"])
            ).total_seconds()
            row["result"] = copy.deepcopy(verification_report)
            row["model_cost_usd"] = 0
            row["infrastructure_cost_usd"] = None
        if integrity_failure is not None:
            save_device_record(job, body.identity.variant_id, record)
            cause = CreatorRenderContractError(
                "I couldn't confirm this export is the file your iPhone uploaded.",
                decline_reason="evidence_missing",
                field_path="export_verification",
                alternative="Retry this same approved edit.",
            )
            await _refuse_publication(
                db,
                job,
                _ContractRefusal(cause, body.identity.variant_id),
                stage="actual_export",
            )
            raise HTTPException(422, "Export does not match the uploaded file")
        if not checked_export_report(record, status, attempt_id):
            if verification_report.get("status") == "checked":
                verification_report["status"] = "rejected"
                verification_report.setdefault("checks", []).append(
                    {"kind": "authority", "status": "failed"}
                )
            else:
                verification_report["status"] = verification_report.get("status") or "partial"
            save_device_record(job, body.identity.variant_id, record)
            from app.kria.automatic_export_repair import repair_failed_export  # noqa: PLC0415

            if not unverified and await repair_failed_export(
                db, job, body.identity.variant_id, attempt_id, verification_report
            ):
                await db.commit()
                return DeviceExportCompleteOut(status="checking", identity=body.identity)
            # Advisory: the finished video still reaches the creator.
            quality_warning = {
                "status": verification_report["status"],
                "failed_checks": sorted(
                    {
                        str(row.get("kind"))
                        for row in verification_report.get("checks", [])
                        if row.get("status") != "passed"
                    }
                ),
            }
    if contract_changes:
        # The approved requirements moved after the phone rendered (`_record` already
        # refused a superseded attempt): advisory, like any other quality finding.
        # The finished video still publishes.
        if verification_report is not None:
            verification_report.setdefault("checks", []).append(
                {"kind": "authority", "status": "failed", "reason": "contract_changed"}
            )
            verification_report["status"] = "rejected"
        failed_checks = (quality_warning or {}).get("failed_checks", [])
        quality_warning = {
            "status": "rejected",
            "failed_checks": sorted({*failed_checks, "contract"}),
        }
    cleanup = (
        await db.execute(
            select(TemporaryMediaUpload)
            .where(TemporaryMediaUpload.object_path == attempt["path"])
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        cleanup is None
        or cleanup.user_id != user_id
        or cleanup.status != "reserved"
        or cleanup.retention_expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(409, "Upload reservation expired")
    record["attempts"][attempt_id]["publication_proof"] = {
        "version": 1,
        "artifact_path": attempt["path"],
        "storage_generation": str(metadata.generation),
        "file_size_bytes": attempt["size"],
        "sha256": attempt["sha256"],
        "identity": status.request.identity.model_dump(mode="json"),
        "source_receipts_digest": sources_digest,
    }
    export_text_notes = _export_text_notes(job, verification_report)
    status.phase = "published"
    record.update(
        status=status.model_dump(mode="json"),
        published_attempt=attempt_id,
        base_generation=attempt_id,
    )
    save_device_record(job, body.identity.variant_id, record)
    assembly = dict(job.assembly_plan)
    assembly["variants"] = [
        {
            # Cloud evidence describes the cloud file this export replaces.
            **{
                key: value
                for key, value in v.items()
                # A note from an earlier export must not outlive a clean re-export.
                if key not in ("cloud_evidence", "export_check_notes")
            },
            **({"export_check_notes": export_text_notes} if export_text_notes else {}),
            "ok": True,
            "render_status": "ready",
            "render_generation_id": attempt_id,
            "render_finished_at": datetime.now(UTC).isoformat(),
            "video_path": attempt["path"],
            # Always overwrite: a poster inherited from an earlier cloud render
            # would show a different video than the one now published.
            "poster_path": poster_path,
            "output_url": url,
            "render_destination": "device",
            **(
                {"actual_export_verification": verification_report}
                if verification_report is not None
                else {}
            ),
            **finished_durations(status.request.recipe.duration, attempt.get("brand_tail", "none")),
            **export_measurements,
            **(
                {
                    "duration_s": export_measurements["measured_export_duration_s"],
                    "measured_export_generation_id": attempt_id,
                }
                if export_measurements
                else {}
            ),
        }
        if v.get("variant_id") == body.identity.variant_id
        else v
        for v in assembly["variants"]
    ]
    job.assembly_plan = assembly
    if all(v.get("ok") for v in assembly["variants"]):
        job.status = "variants_ready"
        job.current_phase = None
        job.finished_at = datetime.now(UTC)
        outcome = _device_speech_cleanup_outcome(assembly, job.id, body.identity.variant_id)
        if outcome is not None:
            job.assembly_plan = {**assembly, "speech_cleanup_outcome": outcome}
    cleanup.status = "attached"
    from app.services.finished_style_learning import record_finished_style_edit

    await record_finished_style_edit(db, user_id, job, body.identity.variant_id, status)
    await record_native_activity(db, job, body.identity.variant_id, kind="video_ready")
    await db.commit()
    if quality_warning is not None:
        # After the commit: the trace write opens its own connection to the Job row.
        await asyncio.to_thread(_record_export_advisory, job_id, body.identity, quality_warning)
    await asyncio.to_thread(_nudge_kria_observer, job_id)
    return DeviceExportCompleteOut(identity=body.identity)
