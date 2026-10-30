"""Native-only opt-in media generation. Completed assets never dispatch a render."""

import uuid

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app.agents._runtime import RunContext
from app.auth import CurrentUser, KriaClientProtocol, NativeClient
from app.config import settings
from app.database import sync_session
from app.models import GeneratedMediaOperation
from app.services.generated_media import (
    GeneratedMediaAccept,
    GeneratedMediaRequest,
    accept_generated_media,
    execute_generated_media,
    generated_media_enabled,
    get_generated_operation,
    narration_script,
    operation_response,
    owned_item,
    retry_generated_storage,
)

router = APIRouter()


def generated_capabilities(user_id, native_client, client_protocol):
    native = bool(
        native_client
        and client_protocol is not None
        and client_protocol >= settings.kria_minimum_client_protocol
        and (settings.phone_rendering_for(user_id) or settings.ios_native_device_only_enabled)
    )
    images = bool(
        native
        and generated_media_enabled("image")
        and "visualBlocks" in settings.phone_render_verified_features
    )
    narration = bool(
        native
        and generated_media_enabled("narration")
        and settings.phone_narration_rendering_enabled
        and "narrationAudio" in settings.phone_render_verified_features
    )
    return {
        "generated_images": images,
        "generated_image_editing": images,
        "generated_narration": narration,
    }


def _native(native_client, client_protocol):
    if not native_client:
        raise HTTPException(404, "Generated assets are available in the iPhone app")
    if client_protocol is None or client_protocol < settings.kria_minimum_client_protocol:
        raise HTTPException(426, "Update Kria to continue")


def _run(action):
    try:
        with sync_session() as db:
            return action(db)
    except ValueError as exc:
        message = str(exc)
        raise HTTPException(404 if "not found" in message.lower() else 409, message) from None


@router.get("/{item_id}/generated-media/narration-script")
async def current_narration_script(
    item_id: uuid.UUID,
    user: CurrentUser,
    native_client: NativeClient = False,
    client_protocol: KriaClientProtocol = None,
):
    _native(native_client, client_protocol)
    return await run_in_threadpool(_run, lambda db: narration_script(db, user.id, item_id))


@router.post("/{item_id}/generated-media")
async def generate_media(
    item_id: uuid.UUID,
    body: GeneratedMediaRequest,
    user: CurrentUser,
    native_client: NativeClient = False,
    client_protocol: KriaClientProtocol = None,
):
    _native(native_client, client_protocol)
    capability = (
        "generated_narration"
        if body.capability == "narration"
        else "generated_image_editing"
        if body.capability == "image_edit"
        else "generated_images"
    )
    if not generated_capabilities(user.id, native_client, client_protocol)[capability]:
        raise HTTPException(404, "Generated media is unavailable")
    ctx = RunContext(
        creator_id=str(user.id), plan_item_id=str(item_id), request_id=str(body.client_event_id)
    )
    return await run_in_threadpool(
        _run,
        lambda db: operation_response(
            execute_generated_media(db, owner_id=user.id, item_id=item_id, body=body, ctx=ctx)
        ),
    )


@router.get("/{item_id}/generated-media/by-event/{client_event_id}")
async def generated_media_by_event(
    item_id: uuid.UUID,
    client_event_id: uuid.UUID,
    user: CurrentUser,
    native_client: NativeClient = False,
    client_protocol: KriaClientProtocol = None,
):
    from sqlalchemy import select

    _native(native_client, client_protocol)

    def read(db):
        owned_item(db, user.id, item_id)
        operation = db.scalar(
            select(GeneratedMediaOperation).where(
                GeneratedMediaOperation.user_id == user.id,
                GeneratedMediaOperation.plan_item_id == item_id,
                GeneratedMediaOperation.client_event_id == str(client_event_id),
            )
        )
        if operation is None:
            raise ValueError("Generated asset not found")
        return operation_response(operation)

    return await run_in_threadpool(_run, read)


@router.get("/{item_id}/generated-media/{operation_id}")
async def generated_media_status(
    item_id: uuid.UUID,
    operation_id: uuid.UUID,
    user: CurrentUser,
    native_client: NativeClient = False,
    client_protocol: KriaClientProtocol = None,
):
    _native(native_client, client_protocol)
    return await run_in_threadpool(
        _run,
        lambda db: operation_response(
            get_generated_operation(db, operation_id, owner_id=user.id, item_id=item_id)
        ),
    )


@router.post("/{item_id}/generated-media/{operation_id}/retry-storage")
async def retry_media_storage(
    item_id: uuid.UUID,
    operation_id: uuid.UUID,
    user: CurrentUser,
    native_client: NativeClient = False,
    client_protocol: KriaClientProtocol = None,
):
    _native(native_client, client_protocol)
    return await run_in_threadpool(
        _run,
        lambda db: operation_response(
            retry_generated_storage(db, operation_id, owner_id=user.id, item_id=item_id)
        ),
    )


@router.post("/{item_id}/generated-media/{operation_id}/accept")
async def accept_media(
    item_id: uuid.UUID,
    operation_id: uuid.UUID,
    body: GeneratedMediaAccept,
    user: CurrentUser,
    native_client: NativeClient = False,
    client_protocol: KriaClientProtocol = None,
):
    _native(native_client, client_protocol)
    return await run_in_threadpool(
        _run,
        lambda db: operation_response(
            accept_generated_media(db, operation_id, owner_id=user.id, item_id=item_id, body=body)
        ),
    )
