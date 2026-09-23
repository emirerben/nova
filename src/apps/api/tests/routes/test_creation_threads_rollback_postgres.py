"""PostgreSQL regression: creation-thread paths that roll back, then read ORM state.

``get_current_user`` loads the ``User`` into the route's own request session
(FastAPI caches ``get_db`` per request).  ``AsyncSession.rollback()`` expires
every instance in that session -- ``expire_on_commit=False`` only covers
commit -- so reading ``user.id`` (or ``thread.id``) after a rollback lazy-loads
outside the greenlet and raises ``MissingGreenlet``.  Every idempotent replay
branch did exactly that, so the client's same-id retry after a failed response
always 500'd (production 2026-09-23 10:39, ``attach_media`` -> ``_load``).

The unit tests missed it because their ``SimpleNamespace`` users never expire;
these use the real dependency on the same real ``AsyncSession``.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import Response
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from starlette.requests import Request

from app.auth import get_current_user
from app.config import settings
from app.database import AsyncSessionLocal, sync_engine
from app.database import engine as async_engine
from app.models import CreationThread, CreationThreadEvent, User
from app.routes.creation_threads import (
    ActionBody,
    ArchiveBody,
    AttachBody,
    MediaInput,
    MessageBody,
    RenameBody,
    _record_partial_variant_retry_enqueue_failure,
    action_thread,
    archive_thread,
    attach_media,
    get_thread,
    message_thread,
    rename_thread,
)

_database_name = make_url(settings.database_url).database or ""
if not _database_name.endswith("_test"):
    pytest.skip(
        f"refusing to write creation-thread integration fixtures to {_database_name!r}",
        allow_module_level=True,
    )
try:
    with sync_engine.connect() as _probe:
        _probe.execute(text("SELECT 1"))
except (OperationalError, OSError) as exc:
    pytest.skip(f"test PostgreSQL unavailable: {exc!r}", allow_module_level=True)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": (f"test-{uuid.uuid4()}", 0),
        }
    )


@pytest.fixture()
async def threads(request: pytest.FixtureRequest):
    """A v1 thread whose events are the receipts each replay below repeats."""

    # pytest-asyncio gives each async test its own loop; drop pooled asyncpg
    # connections created on an earlier loop.
    await async_engine.dispose()
    request.addfinalizer(lambda: async_engine.sync_engine.dispose(close=False))
    user_id, v1_id, v2_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(User(id=user_id, email=f"rollback-expiry-{user_id}@example.test"))
        await db.flush()
        db.add_all(
            [
                CreationThread(
                    id=v1_id, creator_id=user_id, state={}, runtime_version=1, revision=5
                ),
                CreationThread(id=v2_id, creator_id=user_id, state={}, runtime_version=2),
            ]
        )
        await db.flush()
        receipts = [
            ("evt-message", "user_message", "make it punchy", None),
            ("evt-media", "media_added", None, {"media": [{"media_id": "m1"}]}),
            (
                "evt-action",
                "action_select_format",
                None,
                {"action": "select_format", "format": "montage"},
            ),
            ("evt-rename", "thread_renamed", None, {"title": "Trip"}),
            ("evt-archive", "thread_archived", None, None),
        ]
        for sequence, (client_id, event_type, content, payload) in enumerate(receipts):
            db.add(
                CreationThreadEvent(
                    thread_id=v1_id,
                    sequence=sequence,
                    client_event_id=client_id,
                    role="user",
                    event_type=event_type,
                    content=content,
                    payload=payload,
                    revision=sequence + 1,
                )
            )
        await db.commit()
    try:
        yield {"user_id": user_id, "v1": v1_id, "v2": v2_id}
    finally:
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(CreationThread).where(CreationThread.creator_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()
        finally:
            await async_engine.dispose()


async def _request_user(db, user_id: uuid.UUID) -> User:
    # The production dependency, on the same session the route receives.
    return await get_current_user(
        x_user_id=str(user_id),
        authorization=f"Bearer {settings.internal_api_key}",
        db=db,
    )


async def test_message_replay_returns_the_thread(threads) -> None:
    async with AsyncSessionLocal() as db:
        user = await _request_user(db, threads["user_id"])
        out = await message_thread(
            _request(),
            str(threads["v1"]),
            MessageBody(
                message="make it punchy", client_event_id="evt-message", expected_revision=0
            ),
            user,
            db,
        )
    assert str(out.id) == str(threads["v1"])


async def test_attach_media_replay_returns_the_thread(threads) -> None:
    async with AsyncSessionLocal() as db:
        user = await _request_user(db, threads["user_id"])
        out = await attach_media(
            _request(),
            str(threads["v1"]),
            AttachBody(
                media=[MediaInput(media_id="m1", kind="video")],
                client_event_id="evt-media",
                expected_revision=0,
            ),
            user,
            db,
        )
    assert str(out.id) == str(threads["v1"])


async def test_action_replay_returns_the_thread(threads) -> None:
    async with AsyncSessionLocal() as db:
        user = await _request_user(db, threads["user_id"])
        out = await action_thread(
            _request(),
            str(threads["v1"]),
            ActionBody(
                action="select_format",
                payload={"format": "montage"},
                client_action_id="evt-action",
                expected_revision=0,
            ),
            user,
            db,
        )
    assert str(out.id) == str(threads["v1"])


async def test_rename_replay_returns_the_thread(threads) -> None:
    async with AsyncSessionLocal() as db:
        user = await _request_user(db, threads["user_id"])
        out = await rename_thread(
            _request(),
            str(threads["v1"]),
            RenameBody(title="Trip", client_event_id="evt-rename", expected_revision=0),
            user,
            db,
        )
    assert str(out.id) == str(threads["v1"])


async def test_archive_replay_returns_the_thread(threads) -> None:
    async with AsyncSessionLocal() as db:
        user = await _request_user(db, threads["user_id"])
        out = await archive_thread(
            _request(),
            str(threads["v1"]),
            ArchiveBody(client_event_id="evt-archive", expected_revision=0),
            user,
            db,
        )
    assert str(out.id) == str(threads["v1"])


async def test_v2_thread_bootstrap_reads_delta_after_releasing_the_lock(
    threads, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    async with AsyncSessionLocal() as db:
        user = await _request_user(db, threads["user_id"])
        out = await get_thread(
            str(threads["v2"]),
            user,
            db,
            Response(),
            after_sequence=None,
            before_sequence=None,
            limit=100,
            projection=None,
        )
    assert out is not None


async def test_partial_variant_enqueue_failure_rereads_the_thread_by_id(threads) -> None:
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, threads["v1"])
        # action_thread commits and refreshes before it enqueues the retry.
        await db.commit()
        await db.refresh(thread)
        await _record_partial_variant_retry_enqueue_failure(
            db,
            thread,
            uuid.uuid4(),
            uuid.uuid4(),
            "variant-a",
            "gen-1",
            RuntimeError("broker down"),
        )
