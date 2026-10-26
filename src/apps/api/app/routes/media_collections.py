"""Native, default-off authorized collection and interval-search baseline."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, KriaClientProtocol, NativeClient
from app.config import settings
from app.database import get_db
from app.schemas.media_collections import (
    CollectionIndexBody,
    CollectionIndexOut,
    CollectionItemsOut,
    CollectionOut,
    CollectionStoriesOut,
    CollectionStoryOut,
    CollectionWrite,
    MomentInspectionBody,
    MomentInspectionOut,
    MomentSearchOut,
    SavedCollectionStoriesOut,
    SemanticSearchBody,
    StoryCorrectionBody,
    StoryGenerationBody,
    StoryReviewBody,
    StorySelectionBody,
)
from app.services import media_collections as collections
from app.services import media_moments


def require_collections(native_client: NativeClient, user: CurrentUser):
    if not settings.media_library_enabled or not settings.media_collection_proposals_for(user.id):
        raise HTTPException(404, "Collection discovery is not enabled")
    if not native_client:
        raise HTTPException(403, "Collections are available in the iOS app")


def _asks_open_questions(client_protocol: int | None) -> bool:
    return (
        settings.camera_roll_story_extras_enabled
        and client_protocol is not None
        and client_protocol >= settings.open_questions_min_client_protocol
    )


def _for_client(value, client_protocol: int | None):
    """Builds that predate the question UI never see the list (they could not answer it)."""
    if not _asks_open_questions(client_protocol):
        for story in getattr(value, "stories", [value]):
            if story.content_plan is not None:
                story.content_plan.open_questions = []
    return value


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


@router.get("/{identifier}/items", response_model=CollectionItemsOut)
async def items(identifier: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)):
    """Per-member progress (queued -> analyzing -> ready, or an honest failure) for polling."""
    from app.services.media_collection_items import collection_items

    return await collection_items(
        db, user.id, await collections.owned_collection(db, user.id, identifier)
    )


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


@router.post("/{identifier}/semantic-search", response_model=MomentSearchOut)
async def semantic_search(
    identifier: uuid.UUID,
    body: SemanticSearchBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    from app.services.media_moment_retrieval import semantic_search as retrieve

    return await retrieve(
        db,
        user.id,
        identifier,
        revision=body.expected_revision,
        query=body.query,
        cache_only=body.cache_only,
    )


@router.post("/{identifier}/inspect", response_model=MomentInspectionOut)
async def inspect(
    identifier: uuid.UUID,
    body: MomentInspectionBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    from app.services.media_moment_inspection import inspect_moment

    return await inspect_moment(db, user.id, identifier, body)


@router.get("/{identifier}/stories", response_model=SavedCollectionStoriesOut)
async def saved_stories(
    identifier: uuid.UUID,
    user: CurrentUser,
    expected_revision: int = Query(ge=1),
    story_ids: list[uuid.UUID] | None = Query(default=None, max_length=20),
    client_protocol: KriaClientProtocol = None,
    db: AsyncSession = Depends(get_db),
):
    from app.services.media_collection_stories import read_saved_stories

    return _for_client(
        await read_saved_stories(db, user.id, identifier, expected_revision, story_ids),
        client_protocol,
    )


@router.post("/{identifier}/stories", response_model=CollectionStoriesOut)
async def stories(
    identifier: uuid.UUID,
    body: StoryGenerationBody,
    user: CurrentUser,
    client_protocol: KriaClientProtocol = None,
    db: AsyncSession = Depends(get_db),
):
    from app.services.media_collection_stories import generate_stories

    return _for_client(await generate_stories(db, user.id, identifier, body), client_protocol)


@router.patch("/{identifier}/stories/{story_id}", response_model=CollectionStoryOut)
async def correct_story(
    identifier: uuid.UUID,
    story_id: uuid.UUID,
    body: StoryCorrectionBody,
    user: CurrentUser,
    client_protocol: KriaClientProtocol = None,
    db: AsyncSession = Depends(get_db),
):
    from app.services.media_collection_stories import correct_story as correct

    return _for_client(await correct(db, user.id, identifier, story_id, body), client_protocol)


@router.post("/{identifier}/stories/{story_id}/reject", response_model=CollectionStoryOut)
async def reject_story(
    identifier: uuid.UUID,
    story_id: uuid.UUID,
    body: StoryReviewBody,
    user: CurrentUser,
    client_protocol: KriaClientProtocol = None,
    db: AsyncSession = Depends(get_db),
):
    from app.services.media_collection_stories import reject_story as reject

    return _for_client(await reject(db, user.id, identifier, story_id, body), client_protocol)


@router.post("/{identifier}/stories/{story_id}/expose", response_model=CollectionStoryOut)
async def expose_story(
    identifier: uuid.UUID,
    story_id: uuid.UUID,
    body: StoryReviewBody,
    user: CurrentUser,
    client_protocol: KriaClientProtocol = None,
    db: AsyncSession = Depends(get_db),
):
    from app.services.media_collection_stories import expose_story as expose

    return _for_client(await expose(db, user.id, identifier, story_id, body), client_protocol)


@router.post("/{identifier}/stories/{story_id}/select")
async def select_story(
    identifier: uuid.UUID,
    story_id: uuid.UUID,
    body: StorySelectionBody,
    user: CurrentUser,
    client_protocol: KriaClientProtocol = None,
    db: AsyncSession = Depends(get_db),
):
    from app.services.media_collection_stories import select_story as select
    from app.tasks.kria_runtime import run_kria_turn

    accepted, publish = await select(
        db,
        user.id,
        identifier,
        story_id,
        body,
        gate_open_questions=_asks_open_questions(client_protocol),
    )
    if publish:
        try:
            run_kria_turn.delay(accepted.turn_id)
        except Exception:
            # The shared reconciler recovers durable pending turns after broker failure.
            pass
    return accepted
