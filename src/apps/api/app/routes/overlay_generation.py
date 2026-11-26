"""Native review plans: a paid Gemini planning call that never generates images or edits video."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.agents._runtime import RunContext
from app.auth import CurrentUser, KriaClientProtocol, NativeClient
from app.database import get_db, sync_session
from app.kria import overlay_set
from app.kria.overlay_set import OverlayComposeBody, OverlaySetEvent, OverlaySetOpen
from app.routes.generated_media import _native, generated_capabilities
from app.services import generated_media
from app.services.ai_usage_headers import paid_call_headers
from app.services.overlay_generation import (
    OverlayPlanRequest,
    OverlayPlanResponse,
    generate_overlay_plan,
)

router = APIRouter()
thread_router = APIRouter()


def _plan_sync(user_id, item_id, body, locale, attribution):
    try:
        with sync_session() as db:
            return generate_overlay_plan(
                db,
                owner_id=user_id,
                locale=locale,
                item_id=item_id,
                body=body,
                ctx=RunContext(
                    creator_id=str(user_id),
                    plan_item_id=str(item_id),
                    job_id=str(body.job_id),
                    request_id=str(uuid.uuid4()),
                    **attribution,
                ),
            )
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 409, str(exc)) from None


@router.post("/{item_id}/overlay-plan", response_model=OverlayPlanResponse)
async def overlay_plan(
    item_id: uuid.UUID,
    body: OverlayPlanRequest,
    user: CurrentUser,
    request: Request,
    native_client: NativeClient = False,
    client_protocol: KriaClientProtocol = None,
):
    _native(native_client, client_protocol)
    # Planning is a paid Gemini call. It rides the same account gate as image generation:
    # native protocol, cost control on, live approval, budgets and the images kill switch.
    if not generated_capabilities(user.id, native_client, client_protocol)["generated_images"]:
        raise HTTPException(404, "Generated media is unavailable")

    attribution = paid_call_headers(request).as_kwargs()
    return await run_in_threadpool(
        _plan_sync, user.id, item_id, body, request.headers.get("accept-language"), attribution
    )


@thread_router.post("/{thread_id}/overlay-sets", response_model=OverlaySetEvent)
async def open_overlay_set(
    thread_id: str,
    body: OverlaySetOpen,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    native_client: NativeClient = False,
    client_protocol: KriaClientProtocol = None,
):
    overlay_set.admit(user.id, native_client, client_protocol)
    return await overlay_set.open_set(db, thread_id, user.id, body.set_id)


@thread_router.post("/{thread_id}/overlay-sets/{set_id}/compose", response_model=OverlaySetEvent)
async def compose_overlay_set(
    thread_id: str,
    set_id: uuid.UUID,
    body: OverlayComposeBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    native_client: NativeClient = False,
    client_protocol: KriaClientProtocol = None,
):
    overlay_set.admit(user.id, native_client, client_protocol)
    return await overlay_set.answer_compose(db, thread_id, user.id, set_id, body)


@thread_router.post("/{thread_id}/overlay-sets/{set_id}/plan", response_model=OverlayPlanResponse)
async def plan_overlay_set(
    thread_id: str,
    set_id: uuid.UUID,
    body: OverlayPlanRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
    native_client: NativeClient = False,
    client_protocol: KriaClientProtocol = None,
):
    """Plan the set's overlays once. The creator's compose answer, not ``body.mode``/``prompt``,
    says what to plan; a repeat returns the stored plan and pays nothing."""
    overlay_set.admit(user.id, native_client, client_protocol, paid=True)
    stored, compose, item_id = await overlay_set.claim_plan(db, thread_id, user.id, set_id)
    if stored is not None:
        return OverlayPlanResponse.model_validate(stored)
    planned = body.model_copy(
        update={"mode": compose["mode"], "prompt": "\n".join(compose["prompts"])}
    )
    try:
        plan = await run_in_threadpool(
            _plan_sync,
            user.id,
            item_id,
            planned,
            request.headers.get("accept-language"),
            paid_call_headers(request).as_kwargs(),
        )
    except BaseException:
        await overlay_set.release_plan_claim(db, thread_id, user.id, set_id)
        raise
    await overlay_set.record_plan(db, thread_id, user.id, set_id, plan.model_dump(mode="json"))
    return plan


@thread_router.post(
    "/{thread_id}/overlay-sets/{set_id}/overlays/{overlay_id}/generate",
    response_model=OverlaySetEvent,
)
async def generate_set_overlay(
    thread_id: str,
    set_id: uuid.UUID,
    overlay_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    native_client: NativeClient = False,
    client_protocol: KriaClientProtocol = None,
):
    """Make one overlay's image, repeat a request that was cut off, or retry a failed one.

    One call serves all three: a relaunch repeats it for every overlay still ``making``."""
    overlay_set.admit(user.id, native_client, client_protocol, paid=True)
    done, prompt, attempt, item_id = await overlay_set.begin_generation(
        db, thread_id, user.id, set_id, overlay_id
    )
    if done is not None:
        return done
    made = await run_in_threadpool(
        lambda: generated_media.generate_image_for_card(
            owner_id=user.id,
            item_id=item_id,
            prompt=prompt,
            event_id=overlay_set.attempt_event_id(str(set_id), str(overlay_id), attempt),
        )
    )
    return await overlay_set.finish_generation(
        db,
        thread_id,
        user.id,
        set_id,
        overlay_id,
        attempt,
        state=made.state,
        operation_id=made.operation_id,
        failure_code=made.failure_code,
    )
