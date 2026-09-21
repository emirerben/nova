"""Where a project with no device footage renders (KRI-121 round 2).

A footage project is a phone project because its clips are analysis proxies.
A project holding only Visuals has no proxy, so nothing on the item says
"iPhone". The creation thread records that instead: a thread created or used
from the native app on an enrolled account carries a device intent, and one
shared rule turns that intent into "render on the iPhone" only while the
project is something the phone can draw entirely.

The manifest, the media digests, the render dispatch and the job builder must
all ask this rule. If one of them disagrees, confirm or dispatch rejects the
project with ``proposal_stale`` / ``invalid_clips`` instead of rendering it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.agents._schemas.edit_format import coerce_edit_format, guided_edit_applicable
from app.config import settings
from app.models import CreationThread, PlanItemAsset
from app.services.phone_rollout import phone_render_supported_formats

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session

    from app.models import PlanItem

DEVICE_INTENT_KEY = "render_destination_intent"
DEVICE_INTENT = "device"

# Same membership as creator_sessions.CREATOR_VISIBLE_ASSET_STATES: a Visual
# counts from the moment it is registered, so the destination cannot flip (and
# move the manifest hash) when its background analysis finishes.
_REGISTERED_POOL_STATES = ("uploaded", "queued", "analyzing", "ready")

_VISUAL_FEATURES = (("image", "stillImages"), ("video", "visualVideos"))


def phone_drawable_visual_kinds() -> frozenset[str]:
    """Visuals kinds the verified device engine can draw right now."""

    verified = settings.phone_render_verified_features
    return frozenset(kind for kind, feature in _VISUAL_FEATURES if feature in verified)


def has_device_intent(thread_state: object) -> bool:
    return isinstance(thread_state, Mapping) and thread_state.get(DEVICE_INTENT_KEY) == (
        DEVICE_INTENT
    )


def with_device_intent(
    thread_state: Mapping[str, Any] | None, *, native_client: bool, user_id: object
) -> dict[str, Any] | None:
    """Return the stamped state, or None when nothing should change.

    Only the native app can show and run a device render, so a web request
    never stamps; an account outside the pilot never stamps either.
    """

    if not native_client or not settings.phone_rendering_for(user_id):
        return None
    if has_device_intent(thread_state):
        return None
    return {**(thread_state or {}), DEVICE_INTENT_KEY: DEVICE_INTENT}


def visuals_only_on_device(
    *,
    user_id: object,
    edit_format: object,
    has_voiceover: bool,
    has_clip_sources: bool,
    pool_kinds: Iterable[str],
    thread_state: object,
) -> bool:
    """Whether a project with no footage renders on the iPhone.

    Anything the phone cannot draw entirely keeps today's cloud route: footage
    in the clip lane (a web upload), a recorded voiceover, or a format without
    a guided phone compiler. Device footage never reaches this rule's True:
    such a project is already a phone project through its proxies.
    """

    if not settings.phone_rendering_for(user_id) or not has_device_intent(thread_state):
        return False
    if has_clip_sources or has_voiceover:
        return False
    if (
        not guided_edit_applicable(edit_format, has_voiceover=False)
        or coerce_edit_format(edit_format) not in phone_render_supported_formats()
    ):
        return False
    drawable = phone_drawable_visual_kinds()
    return any(kind in drawable for kind in pool_kinds)


def _item_inputs(item: PlanItem) -> tuple[bool, bool]:
    """(has_clip_sources, has_voiceover), read exactly as dispatch reads them."""

    has_clip_sources = bool(list(getattr(item, "clip_gcs_paths", None) or [])) or any(
        isinstance(row, dict) and row.get("gcs_path")
        for row in (getattr(item, "clip_assignments", None) or [])
    )
    has_voiceover = getattr(item, "audio_mode", None) == "voiceover" and bool(
        getattr(item, "voiceover_gcs_path", None)
    )
    return has_clip_sources, has_voiceover


def _could_apply(item: PlanItem, owner_id: object) -> bool:
    """Cheap, query-free half of the rule; most items stop here."""

    has_clip_sources, has_voiceover = _item_inputs(item)
    return (
        bool(settings.phone_rendering_for(owner_id))
        and bool(phone_drawable_visual_kinds())
        and not has_clip_sources
        and not has_voiceover
    )


def _thread_states_query(item_id: uuid.UUID, owner_id: object):  # noqa: ANN202
    # A plain read: creation-thread routes lock the thread row BEFORE item
    # locks, and callers here may already hold the item lock.
    return select(CreationThread.state).where(
        CreationThread.active_plan_item_id == item_id,
        CreationThread.creator_id == owner_id,
    )


def _pool_kinds_query(item_id: uuid.UUID, owner_id: object):  # noqa: ANN202
    return (
        select(PlanItemAsset.kind)
        .where(
            PlanItemAsset.plan_item_id == item_id,
            PlanItemAsset.user_id == owner_id,
            PlanItemAsset.status.in_(_REGISTERED_POOL_STATES),
            PlanItemAsset.deduplicated_to_asset_id.is_(None),
        )
        .distinct()
    )


def _decide(item: PlanItem, owner_id: object, states: list, kinds: list) -> bool:
    has_clip_sources, has_voiceover = _item_inputs(item)
    return visuals_only_on_device(
        user_id=owner_id,
        edit_format=getattr(item, "edit_format", None),
        has_voiceover=has_voiceover,
        has_clip_sources=has_clip_sources,
        pool_kinds=[kind for kind in kinds if isinstance(kind, str)],
        # A project normally has one thread; any of them carrying the intent
        # means the creator works on it from the iPhone.
        thread_state=next((state for state in states if has_device_intent(state)), None),
    )


async def item_visuals_only_on_device(
    db: AsyncSession,
    item: PlanItem,
    owner_id: object,
    *,
    thread_state: Mapping[str, Any] | None = None,
) -> bool:
    """The shared rule for one plan item (async routes).

    A caller that already holds the item's locked thread passes its state, so
    a stamp made in the same request counts without another read.
    """

    if not _could_apply(item, owner_id):
        return False
    states = (
        [thread_state]
        if thread_state is not None
        else list((await db.execute(_thread_states_query(item.id, owner_id))).scalars())
    )
    if not any(has_device_intent(state) for state in states):
        return False
    kinds = list((await db.execute(_pool_kinds_query(item.id, owner_id))).scalars())
    return _decide(item, owner_id, states, kinds)


def item_visuals_only_on_device_sync(session: Session, item: PlanItem, owner_id: object) -> bool:
    """The shared rule for one plan item (Celery tasks)."""

    if not _could_apply(item, owner_id):
        return False
    states = list(session.execute(_thread_states_query(item.id, owner_id)).scalars())
    if not any(has_device_intent(state) for state in states):
        return False
    kinds = list(session.execute(_pool_kinds_query(item.id, owner_id)).scalars())
    return _decide(item, owner_id, states, kinds)
