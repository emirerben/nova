"""Explicit account calibration and creative feedback, separate from render approval."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.database import get_db
from app.schemas.editorial_taste import IdeaExampleWrite, IdeaFeedbackWrite, TasteErase, TasteWrite
from app.services import creator_editorial_taste as service


def available(user: CurrentUser):
    service.enabled(user.id)


router = APIRouter(
    prefix="/me/editorial-taste", tags=["editorial-taste"], dependencies=[Depends(available)]
)


@router.get("")
async def read(user: CurrentUser, db: AsyncSession = Depends(get_db)):
    return await service.read_profile(db, user.id)


@router.put("")
async def write(body: TasteWrite, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    result = await service.write_profile(db, user.id, body)
    await db.commit()
    return result


@router.delete("")
async def erase(body: TasteErase, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    result = await service.erase_profile(
        db, user.id, key=body.idempotency_key, expected_revision=body.expected_revision
    )
    await db.commit()
    return result


@router.post("/feedback")
async def feedback(body: IdeaFeedbackWrite, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    result = await service.record_feedback(db, user.id, body)
    await db.commit()
    return result


@router.post("/feedback/{feedback_id}/example")
async def idea_example(
    feedback_id: UUID,
    body: IdeaExampleWrite,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    result = await service.confirm_idea_example(db, user.id, feedback_id, body)
    await db.commit()
    return result
