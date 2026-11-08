"""KRI-1075: renaming the chat while Kria plans must not throw the finished plan away.

Prod thread 5dda77f9 (Leverkusen kit M1 run 1, 2026-10-30, Fly 1deead562): the creator
sent the M1 prompt at 14:50:24 (rev 12, turn a8da158a). The automatic title landed at
14:50:25 (rev 13), then the creator renamed the chat from the chat list twice, a typo at
14:50:38 (rev 14) and its fix at 14:50:42 (rev 15). The first main-creator plan finished
at 14:51:07 (43 s, $0.150), but `run_kria_turn` returned `requeued` because the commit
fence counted the renames as a conversation change. The second plan (42 s, $0.094)
answered at 14:52:06: 1 min 42 s for the first reply, half of it thrown away.

A chat name is display-only: no planner input reads `CreationThread.title` (the video
title comes from the creator's message), so a rename carries nothing a turn could act on.

These tests replay that event order against Postgres through the real title writer, the
real rename route and the real worker commit.

Failure modes pinned here:
- one or several renames during planning still requeue the turn (a second planning pass);
- tolerating renames hides a real change that lands in the same gap (a clip added), which
  must still re-plan;
- the creator's chosen name is lost after the turn commits;
- after the commit, the next message from the iPhone (fresh revision) is refused.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from app.auth import get_current_user
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.runtime import read_delta
from app.models import CreationThread, CreationThreadEvent, CreatorAgentTurn
from app.routes.creation_threads import RenameBody, rename_thread
from app.services.creation_thread_titles import generate_thread_title
from app.tasks.kria_runtime import run_kria_turn

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

from tests.kria.test_kri552_duplicate_send_postgres import (  # noqa: E402
    _SUMMARY,
    _append,
    _planned,
    _send,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project  # noqa: E402

# The M1 prompt of prod rev 12, word for word.
_MESSAGE = (
    "Montage of my night at the BayArena: my friend Taha invited me to his VIP box for Bayer "
    "Leverkusen vs 1. FC Nürnberg in the DFB-Pokal. Put everything in the order I filmed it, "
    "starting with the train ride. Make it all vertical, no black bars. Skip the photo of the "
    "empty train seat, and use only one of the two photos of the view from our box. Keep the "
    'sound only on the train clip with the announcement "Nächster Halt: Leverkusen Mitte" and '
    'mute everything else. Title: "Pokalnacht in Leverkusen 🔴⚫". Make it 25 seconds. Suggest '
    "a song but don't add it."
)
_AUTO_TITLE = "Pokalnacht in Leverkusen 🔴⚫"
_TYPO = "Pokalnacht in Leverkuseb"
_FIXED = "Pokalnacht in Leverkusen"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


@pytest.fixture()
def republished(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Turn ids the worker hands back to the queue (a requeue or a successor)."""
    sent: list[str] = []
    monkeypatch.setattr(
        run_kria_turn, "apply_async", lambda *, args, **_kwargs: sent.append(args[0])
    )
    return sent


def _untitled_chat() -> tuple[uuid.UUID, uuid.UUID]:
    """A runtime-v2 chat that has not been named yet, as a new iPhone chat starts."""
    user_id, thread_id, _session = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.title = "Untitled video"
        db.commit()
    return user_id, thread_id


async def _rename(sessions, user_id: uuid.UUID, thread_id: uuid.UUID, title: str) -> None:  # noqa: ANN001
    """The chat list renaming the chat, with the revision it last polled."""
    async with sessions() as db:
        thread = await db.get(CreationThread, thread_id)
        seen = int(thread.revision)
        await db.rollback()
        user = await get_current_user(
            x_user_id=str(user_id),
            authorization=f"Bearer {settings.internal_api_key}",
            db=db,
        )
        await rename_thread(
            Request(
                {
                    "type": "http",
                    "method": "PATCH",
                    "path": "/",
                    "headers": [],
                    "client": (f"test-{uuid.uuid4()}", 0),
                }
            ),
            str(thread_id),
            RenameBody(
                title=title, expected_revision=seen, client_event_id=f"kri1075-{uuid.uuid4().hex}"
            ),
            user,
            db,
        )


async def _while_planning(user_id: uuid.UUID, thread_id: uuid.UUID, *, add_clip: bool) -> None:
    """Prod revs 13–15 from inside the planner: the auto title, then two renames.

    The worker's event loop owns no pooled engine, so this uses its own.
    """
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def _summarize(_prompt: str) -> str:
        return _AUTO_TITLE

    try:
        with (
            patch("app.services.creation_thread_titles.AsyncSessionLocal", sessions),
            patch("app.services.creation_thread_titles._summarize", _summarize),
        ):
            await generate_thread_title(thread_id)
        for title in (_TYPO, _FIXED):
            await _rename(sessions, user_id, thread_id, title)
    finally:
        await engine.dispose()
    if add_clip:
        with sync_session() as db:
            thread = db.get(CreationThread, thread_id)
            _append(
                db,
                thread,
                role="user",
                event_type="media_added",
                content=None,
                payload={"media": [{"media_id": "clip-2", "filename": "goal.mov"}]},
            )
            db.commit()


def _gap_events(thread_id: uuid.UUID) -> list[tuple[int, str]]:
    with sync_session() as db:
        return [
            (int(revision), kind)
            for revision, kind in db.execute(
                select(CreationThreadEvent.revision, CreationThreadEvent.event_type)
                .where(CreationThreadEvent.thread_id == thread_id)
                .order_by(CreationThreadEvent.sequence)
            )
        ]


@pytest.mark.asyncio
async def test_two_renames_while_kria_plans_keep_the_first_plan(
    monkeypatch: pytest.MonkeyPatch, republished: list[str]
) -> None:
    """Prod revs 12–16: auto title, typo rename, fixed rename, then one committed reply."""
    user_id, thread_id = _untitled_chat()
    calls: list[int] = []

    async def _planner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append(1)
        if len(calls) == 1:
            await _while_planning(user_id, thread_id, add_clip=False)
        return _planned()

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planner)
    turn = await _send(user_id, thread_id, _MESSAGE)

    result = await asyncio.to_thread(run_kria_turn.run, turn.turn_id)

    # The prod order really is in the gap: the user message, the auto title, two renames.
    events = _gap_events(thread_id)
    message_revision = next(r for r, kind in events if kind == "user_message")
    assert [kind for r, kind in events if r > message_revision][:3] == [
        "thread_title_generated",
        "thread_renamed",
        "thread_renamed",
    ]
    # One planning pass, committed; nothing handed back to the queue.
    assert result == {"turn_id": turn.turn_id, "status": "awaiting_approval"}
    assert len(calls) == 1
    assert republished == []
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        # The creator's last choice of name survives the commit.
        assert thread.title == _FIXED
        assert (thread.state or {}).get("title_source") == "user"
        statuses = list(
            db.execute(
                select(CreatorAgentTurn.status).where(CreatorAgentTurn.thread_id == thread_id)
            ).scalars()
        )
    assert statuses == ["awaiting_approval"]
    # The iPhone's next message, sent with the revision it now sees, is accepted.
    follow_up = await _send(user_id, thread_id, "Make it 20 seconds instead.")
    assert follow_up.turn_id != turn.turn_id


@pytest.mark.asyncio
async def test_a_clip_added_alongside_the_renames_still_replans(
    monkeypatch: pytest.MonkeyPatch, republished: list[str]
) -> None:
    """Control: tolerating a rename must not hide a real change in the same gap."""
    user_id, thread_id = _untitled_chat()
    calls: list[int] = []

    async def _planner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append(1)
        if len(calls) == 1:
            await _while_planning(user_id, thread_id, add_clip=True)
        return _planned()

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planner)
    turn = await _send(user_id, thread_id, _MESSAGE)

    result = await asyncio.to_thread(run_kria_turn.run, turn.turn_id)

    assert result == {"turn_id": turn.turn_id, "status": "requeued"}
    assert republished == [turn.turn_id]
    with sync_session() as db:
        assert db.get(CreatorAgentTurn, uuid.UUID(turn.turn_id)).status == "pending"


# KRI-1262: a chat that opened with the stock "Suggest an edit." is named from the plan.
#
# Failure modes: the stock prompt becoming the title; a title generated before any plan; the
# plan never arriving (the placeholder stays, nothing breaks); a rename before the plan being
# overwritten by the generated name (KRI-1075: the creator's name always wins); the generated
# title's revision bump throwing away the plan in flight; a second draft commit renaming again.
_STOCK = "Suggest an edit."


async def _generate_title(summaries: list[tuple[str, bool]], thread_id: uuid.UUID) -> None:
    """`generate_thread_title` the way the API runs it, with the model stubbed."""
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def _summarize(text: str, *, from_plan: bool = False) -> str:
        summaries.append((text, from_plan))
        return "Whisk To Order"

    try:
        with (
            patch("app.services.creation_thread_titles.AsyncSessionLocal", sessions),
            patch("app.services.creation_thread_titles._summarize", _summarize),
        ):
            await generate_thread_title(thread_id)
    finally:
        await engine.dispose()


def _title_state(thread_id: uuid.UUID) -> tuple[str, dict]:
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        return thread.title, {
            k: v for k, v in (thread.state or {}).items() if k.startswith("title_")
        }


@pytest.mark.asyncio
async def test_a_stock_prompt_chat_is_named_from_the_plan_once_the_planner_has_one(
    monkeypatch: pytest.MonkeyPatch, republished: list[str]
) -> None:
    user_id, thread_id = _untitled_chat()
    monkeypatch.setattr(
        "app.tasks.kria_runtime._plan_with_live_agent", lambda *_a, **_k: _async(_planned())
    )
    turn = await _send(user_id, thread_id, _STOCK)

    title, state = _title_state(thread_id)
    assert title == "Untitled video" and state["title_generation"] == "deferred"
    summaries: list[tuple[str, bool]] = []
    with patch("app.kria.runtime.start_title_generation") as started:
        async with AsyncSessionLocal() as db:
            await read_delta(
                db, thread_id=thread_id, creator_id=user_id, after_sequence=-1, limit=5
            )
    started.assert_not_called()
    await _generate_title(summaries, thread_id)
    assert summaries == [] and _title_state(thread_id)[0] == "Untitled video"

    result = await asyncio.to_thread(run_kria_turn.run, turn.turn_id)

    assert result["status"] == "awaiting_approval"
    title, state = _title_state(thread_id)
    assert title == "Untitled video" and state["title_generation"] == "pending"
    # The worker cannot run the API's naming task, so the creator's next poll starts it.
    with patch("app.kria.runtime.start_title_generation") as started:
        async with AsyncSessionLocal() as db:
            await read_delta(
                db, thread_id=thread_id, creator_id=user_id, after_sequence=-1, limit=5
            )
    started.assert_called_once_with(thread_id)
    await _generate_title(summaries, thread_id)
    assert summaries == [(_SUMMARY, True)]
    title, state = _title_state(thread_id)
    assert title == "Whisk To Order"
    assert (state["title_source"], state["title_generation"]) == ("generated", "completed")
    assert "title_plan_text" not in state
    with sync_session() as db:
        kinds = [
            e.event_type
            for e in db.scalars(
                select(CreationThreadEvent).where(CreationThreadEvent.thread_id == thread_id)
            )
        ]
    assert kinds.count("thread_title_generated") == 1
    assert republished == []
    # The title landing after the draft must not make the creator's next tap stale.
    follow_up = await _send(user_id, thread_id, "Make it 20 seconds instead.")
    assert follow_up.turn_id != turn.turn_id


@pytest.mark.asyncio
async def test_a_rename_while_a_stock_prompt_chat_plans_keeps_the_creators_name(
    monkeypatch: pytest.MonkeyPatch, republished: list[str]
) -> None:
    user_id, thread_id = _untitled_chat()

    async def _planner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
        try:
            await _rename(
                async_sessionmaker(engine, expire_on_commit=False), user_id, thread_id, _FIXED
            )
        finally:
            await engine.dispose()
        return _planned()

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planner)
    turn = await _send(user_id, thread_id, _STOCK)

    result = await asyncio.to_thread(run_kria_turn.run, turn.turn_id)

    assert result == {"turn_id": turn.turn_id, "status": "awaiting_approval"}
    assert republished == []
    title, state = _title_state(thread_id)
    assert title == _FIXED
    assert (state["title_source"], state["title_generation"]) == ("user", "deferred")
    summaries: list[tuple[str, bool]] = []
    await _generate_title(summaries, thread_id)
    assert summaries == [] and _title_state(thread_id)[0] == _FIXED


@pytest.mark.asyncio
async def test_a_second_draft_does_not_rename_a_named_stock_prompt_chat(
    monkeypatch: pytest.MonkeyPatch, republished: list[str]
) -> None:
    user_id, thread_id = _untitled_chat()
    monkeypatch.setattr(
        "app.tasks.kria_runtime._plan_with_live_agent", lambda *_a, **_k: _async(_planned())
    )
    first = await _send(user_id, thread_id, _STOCK)
    await asyncio.to_thread(run_kria_turn.run, first.turn_id)
    await _generate_title([], thread_id)
    named = _title_state(thread_id)

    second = await _send(user_id, thread_id, "Make it 20 seconds instead.")
    await asyncio.to_thread(run_kria_turn.run, second.turn_id)

    assert _title_state(thread_id) == named
    assert named[0] == "Whisk To Order"


async def _async(value):  # noqa: ANN001, ANN202
    return value
