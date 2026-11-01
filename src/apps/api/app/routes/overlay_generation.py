"""Native review plans: a paid Gemini planning call that never generates images or edits video."""

import uuid

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.agents._runtime import RunContext
from app.auth import CurrentUser, KriaClientProtocol, NativeClient
from app.database import sync_session
from app.routes.generated_media import _native, generated_capabilities
from app.services.overlay_generation import (
    OverlayPlanRequest,
    OverlayPlanResponse,
    generate_overlay_plan,
)

router = APIRouter()


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

    def run():
        try:
            with sync_session() as db:
                return generate_overlay_plan(
                    db,
                    owner_id=user.id,
                    locale=request.headers.get("accept-language"),
                    item_id=item_id,
                    body=body,
                    ctx=RunContext(
                        creator_id=str(user.id),
                        plan_item_id=str(item_id),
                        job_id=str(body.job_id),
                        request_id=str(uuid.uuid4()),
                    ),
                )
        except ValueError as exc:
            raise HTTPException(404 if "not found" in str(exc).lower() else 409, str(exc)) from None

    return await run_in_threadpool(run)
