"""A prod-shaped one-clip iPhone Talking thread for real-Postgres planner tests.

Seeds the shape of KRI-238's prod thread 467a02c4 (2026-10-01): a `subtitled`
item carrying the clip's iOS upload receipt, then the same thread events, ending
in "Add captions". Pair it with the `prod_profile` fixture.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import sync_session
from app.kria import planner
from app.kria.planner import PlannedKriaTurn
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreatorAgentSession,
    Persona,
    PlanItem,
    User,
)

MEDIA_ID = "analysis-proxy-ios-4DB8A9C3-80FE-4269-9D22-7C19280DC543.mp4"
ADD_CAPTIONS = "Add captions"
# The receipt iOS uploaded with the clip (prod `media_added` payload).
UPLOAD_CONTRACT = {
    "proxy": {
        "width": 320,
        "height": 568,
        "original": {
            "kind": "video",
            "width": 1920,
            "height": 1080,
            "sha256": "727797c7fd0cf096191ff17f3dc9a500048083efaa7acd019c2fe35f9e354dbc",
            "has_audio": True,
            "byte_count": 39352254,
            "duration_s": 14.793333333333333,
            "orientation_degrees": 90,
        },
        "duration_s": 14.8,
        "frame_rate": 30.0,
        "timing_version": 1,
        "orientation_degrees": 0,
    },
    "purpose": "analysis_proxy",
}


def seed_talking_thread(
    *, history: list[tuple[str, str]] = ()
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Commit the thread; returns (creator id, thread id, item id).

    `history` is (role, text) chat turns placed before the final "Add captions".
    """
    user_id, persona_id, plan_id, item_id, session_id, thread_id = (uuid.uuid4() for _ in range(6))
    path = f"users/{user_id}/creation-threads/{thread_id}/analysis-proxies/{MEDIA_ID}"
    with sync_session() as db:
        db.add(User(id=user_id, email=f"{user_id}@test.local"))
        db.flush()
        db.add(Persona(id=persona_id, user_id=user_id, persona_status="ready", persona={}))
        db.flush()
        db.add(ContentPlan(id=plan_id, user_id=user_id, persona_id=persona_id))
        db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan_id,
                position=1,
                idea="",
                item_status="awaiting_clips",
                edit_format="subtitled",
                content_mode="existing_footage",
                clip_gcs_paths=[path],
                clip_assignments=[
                    {
                        "media_id": MEDIA_ID,
                        "manifest_identity": MEDIA_ID,
                        "gcs_path": path,
                        "kind": "video",
                        "duration_s": 14.8,
                        "has_audio": True,
                        "storage_generation": "1790858000000000",
                        "upload_contract": UPLOAD_CONTRACT,
                    }
                ],
            )
        )
        db.flush()
        db.add(CreatorAgentSession(id=session_id, creator_id=user_id, plan_item_id=item_id))
        db.flush()
        db.add(
            CreationThread(
                id=thread_id,
                creator_id=user_id,
                runtime_version=2,
                content_plan_id=plan_id,
                active_plan_item_id=item_id,
                active_creator_agent_session_id=session_id,
                title="Add Captions",
                revision=5,
                state={"edit_format": "talking_to_camera", "media_count": 1},
            )
        )
        db.flush()
        events = [
            ("system", "thread_created", None),
            ("assistant", "format_prompt", "What are we making? Pick a format."),
            ("user", "action_select_format", None),
            ("user", "media_added", None),
            *((role, f"{role}_message", text) for role, text in history),
            ("user", "user_message", ADD_CAPTIONS),
        ]
        for sequence, (role, kind, content) in enumerate(events):
            db.add(
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=sequence,
                    revision=sequence + 1,
                    role=role,
                    event_type=kind,
                    content=content,
                    payload=None,
                )
            )
        db.commit()
    return user_id, thread_id, item_id


async def plan_add_captions_turn(
    user_id: uuid.UUID, thread_id: uuid.UUID, item_id: uuid.UUID
) -> PlannedKriaTurn:
    """Plan the thread's "Add captions" turn through the real runtime-v2 planner."""
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            return await planner.plan_live_turn(
                db,
                thread_id=thread_id,
                item_id=item_id,
                creator_id=user_id,
                user_message=ADD_CAPTIONS,
            )
    finally:
        await engine.dispose()
