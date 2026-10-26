"""Removing one event (group) from a collection without disturbing the others."""

import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, select

from app.models import MediaCollectionMember, MediaCollectionStory
from app.schemas.media_collections import GroupDeleteBody, GroupDeleteOut
from app.services import media_collections
from app.services.media_collection_stories import is_deleted, soft_delete


async def delete_group(db, owner: uuid.UUID, identifier: uuid.UUID, body: GroupDeleteBody):
    """Drop the event's members and soft-delete its ideas; other events keep both.

    The collection revision bumps. Other events' members are untouched, so their ideas move
    to the new revision (they would otherwise be orphaned by it). The client undoes this by
    saving the members again and restoring each returned idea.
    """
    await media_collections.lock_library_owner(db, owner)
    row = await media_collections.owned_collection(
        db, owner, identifier, revision=body.expected_revision
    )
    selected = media_collections.members(row)
    removed = [member for member in selected if member.group == body.group]
    if not removed:
        raise HTTPException(404, "Event not found in this collection")
    remaining = [member for member in selected if member.group != body.group]
    previous_revision = row.revision
    row.revision += 1
    row.selection_json = {
        "request_digest": hashlib.sha256(
            json.dumps(
                {"delete_group": body.group, "expected_revision": body.expected_revision},
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "members": [member.model_dump(mode="json") for member in remaining],
    }
    await db.execute(
        delete(MediaCollectionMember).where(
            MediaCollectionMember.collection_id == identifier,
            MediaCollectionMember.user_id == owner,
            MediaCollectionMember.asset_id.in_([member.asset_id for member in removed]),
        )
    )
    deleted = []
    for story in (
        await db.scalars(
            select(MediaCollectionStory)
            .where(
                MediaCollectionStory.user_id == owner,
                MediaCollectionStory.collection_id == identifier,
                MediaCollectionStory.story_json["group"].astext == body.group,
            )
            .with_for_update()
        )
    ).all():
        if not is_deleted(story):
            soft_delete(story, reason="group")
            deleted.append(story.id)
    await media_collections.carry_forward_stories(
        db,
        owner,
        identifier,
        from_revision=previous_revision,
        to_revision=row.revision,
        groups=media_collections.unchanged_groups(selected, remaining) - {body.group},
    )
    await db.commit()
    return GroupDeleteOut(revision=row.revision, deleted_story_ids=deleted)
