"""KRI-1633: ``POST /plan-items/{item_id}/variants/{variant_id}/intro-styles``.

The editor's "Try intro styles": the server FITS several intro looks to this video's own title and
evidence and answers; it computes and STORES NOTHING. The editor stages the tapped option's rows
and her normal editor-commit Save persists them as ordinary title rows (the KRI-1501 caption
translation precedent). No model is called. The module's failure-mode list is in
`app/pipeline/intro_styles.py`.

Gates, in order: 404 ``intro_styles_disabled`` (flag / account), 426 below the minimum build,
the usual 404 for an item or variant that is not hers, 409 ``baseline_conflict`` (the editor's
own problem body), 422 ``intro_styles_title_rows_invalid``. Rate limits 10/minute per IP and
60/hour per caller (429).
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.limiter import limiter
from app.routes.waitlist import get_real_ip

log = structlog.get_logger(__name__)

router = APIRouter()


class IntroStylesRequest(BaseModel):
    model_config = {"extra": "forbid"}
    version: int = Field(ge=1, le=1)
    # The baseline the editor loaded: the same string editor-commit sends.
    base_generation: str = Field(max_length=200)
    # The editor's CURRENT title-family rows (unsaved edits included). Validated by the offer so
    # the failure carries this route's own code, not a generic schema error.
    title_rows: Any = None


def _rate_key(request: Request) -> str:
    from app.routes.plan_items import _edit_conversation_rate_key  # noqa: PLC0415

    return _edit_conversation_rate_key(request)


@router.post("/{item_id}/variants/{variant_id}/intro-styles")
@limiter.limit("10/minute", key_func=get_real_ip)
@limiter.limit("60/hour", key_func=_rate_key)
async def intro_styles_item(
    request: Request,
    item_id: str,
    variant_id: str,
    body: IntroStylesRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Any:
    from app.pipeline import intro_styles as styles  # noqa: PLC0415
    from app.routes import plan_items as items  # noqa: PLC0415
    from app.routes.generative_jobs import variant_render_baseline  # noqa: PLC0415
    from app.services.client_protocol import current_client_protocol  # noqa: PLC0415
    from app.services.intro_card_pool import load_card_photos  # noqa: PLC0415
    from app.services.talking_music import UploadedSongRead, uploaded_song  # noqa: PLC0415

    if not settings.intro_scene_for(user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "intro_styles_disabled"})
    if not styles.protocol_allows(current_client_protocol()):
        raise HTTPException(
            status.HTTP_426_UPGRADE_REQUIRED,
            detail={
                "code": "client_update_required",
                "feature": "intro_styles",
                "minimum_client_protocol": settings.intro_styles_min_client_protocol,
            },
        )
    job = await items._owned_item_render_job(item_id, user.id, db)
    variant = items._find_variant(job, variant_id)
    if variant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Variant not found")
    if body.base_generation != variant_render_baseline(variant):
        code = "baseline_conflict"
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": code,
                "problem": {
                    "code": code,
                    "recovery": items._editor_commit_problem_recovery(code),
                    "current_base_generation": variant_render_baseline(variant),
                },
            },
        )
    # Everything the dry-run Save reads from the DB, fetched now; then the session is released
    # and the Skia work runs in a thread on a detached copy (F18).
    item = await items._load_owned_item(item_id, user.id, db)
    save_arguments = {
        "user_id": str(user.id),
        "plan_item_id": str(item.id),
        "uploaded_song": UploadedSongRead(uploaded_song(item)),
        "phone_sfx_catalog_paths": await items._phone_subtitled_sfx_paths(db, job, variant),
    }
    # KRI-1615: her card photos, only for a build that parses cards with the switch on (M31).
    photos = (
        await load_card_photos(db, item.id, user.id)
        if styles.cards_allowed(current_client_protocol())
        and variant.get("resolved_archetype") == "subtitled"
        else []
    )
    detached = styles.detach(job)
    await db.rollback()  # release the connection before the CPU work
    try:
        offer = await run_in_threadpool(
            styles.build_offer,
            detached,
            variant_id,
            body.title_rows,
            body.base_generation,
            save_arguments=save_arguments,
            photos=photos,
        )
    except styles.TitleRowsInvalid as exc:
        log.info("intro_styles.rows_invalid", reason=str(exc))
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "intro_styles_title_rows_invalid"}
        ) from exc
    return JSONResponse(content=jsonable_encoder(offer))
