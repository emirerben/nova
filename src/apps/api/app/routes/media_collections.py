"""Native, default-off authorized collection and interval-search baseline."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, NativeClient
from app.config import settings
from app.database import get_db
from app.schemas.media_collections import (
    CollectionIndexBody,
    CollectionIndexOut,
    CollectionOut,
    CollectionWrite,
    MomentSearchOut,
)
from app.services import media_collections as collections
from app.services import media_moments


def require_collections(native_client: NativeClient):
    if not settings.media_library_enabled or not settings.media_collection_proposals_enabled:
        raise HTTPException(404, "Collection discovery is not enabled")
    if not native_client:
        raise HTTPException(403, "Collections are available in the iOS app")


router = APIRouter(
    prefix="/me/media-collections",
    tags=["media-library"],
    dependencies=[Depends(require_collections)],
)


@router.put("/{identifier}", response_model=CollectionOut)
async def write(
    identifier: uuid.UUID,
    body: CollectionWrite,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    return await collections.write_collection(db, user.id, identifier, body)


@router.get("/{identifier}", response_model=CollectionOut)
async def read(identifier: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    row = await collections.owned_collection(db, user.id, identifier)
    available = await collections.eligible_sources(db, user.id, collections.members(row))
    return collections.collection_out(row, available)


@router.post("/{identifier}/index", response_model=CollectionIndexOut)
async def index(
    identifier: uuid.UUID,
    body: CollectionIndexBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    return await media_moments.index_collection(db, user.id, identifier, body)


@router.get("/{identifier}/moments", response_model=MomentSearchOut)
async def search(
    identifier: uuid.UUID,
    user: CurrentUser,
    expected_revision: int = Query(ge=0),
    query: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=10, ge=1, le=10),
    db: AsyncSession = Depends(get_db),
):
    return await media_moments.search_moments(
        db, user.id, identifier, revision=expected_revision, query=query, limit=limit
    )


@router.get("/{identifier}/sources/{source_id}/transcript", response_model=MomentSearchOut)
async def transcript_range(
    identifier: uuid.UUID,
    source_id: uuid.UUID,
    user: CurrentUser,
    expected_revision: int = Query(ge=1),
    start_s: float = Query(ge=0, le=1800, allow_inf_nan=False),
    end_s: float = Query(gt=0, le=1800, allow_inf_nan=False),
    db: AsyncSession = Depends(get_db),
):
    if not start_s < end_s or end_s - start_s > 60:
        raise HTTPException(422, "Read a transcript range of at most 60 seconds")
    return await media_moments.search_moments(
        db,
        user.id,
        identifier,
        revision=expected_revision,
        query="",
        limit=100,
        source_id=source_id,
        start_s=start_s,
        end_s=end_s,
    )
