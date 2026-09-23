"""PostgreSQL regression: linking a new Creator session to its chat thread must
not deadlock with the clip-preparation worker it just enqueued.

Production, 2026-09-23 (thread B8D898EC, session 7c7fba26): the first chat
message committed the Creator session and published ``prepare_creator_clips``.
``_agent_message`` then linked the session in a fresh transaction:

* UPDATE #1 (autoflush of ``active_creator_agent_session_id``) takes the thread
  row lock plus a FOR KEY SHARE on the new ``creator_agent_sessions`` row (the
  only FK column that changed);
* ``_sync_agent`` -> ``_append`` bumps ``revision`` = UPDATE #2 of the same row
  in the same transaction. PostgreSQL re-runs the RI check of EVERY foreign key
  on a row this transaction already updated (``RI_FKey_fk_upd_check_required``:
  xmin is the current transaction), so it now needs FOR KEY SHARE on the
  ``plan_items`` row (and ``content_plans``) too.

Meanwhile the worker's ``_locked`` holds ``content_plans`` + ``plan_items`` and
waits for FOR UPDATE on the session the API already key-shares. The API request
was the deadlock victim, so the thread never linked and the chat showed
nothing. ``content_plans`` is locked FOR NO KEY UPDATE since #1178, which no
longer blocks the RI check; ``plan_items`` still is FOR UPDATE.

The two sides are real code: ``message_thread`` (real Creator controller and
``maybe_prepare``) and the real ``prepare_creator_clips`` claim. The only
instrumentation is a barrier on each side, so the interleaving is the
production one on every run instead of a timing race. Each barrier waits for a
condition the other side reaches with either lock order, so a correctly ordered
implementation passes through them without hanging.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Callable
from typing import NamedTuple

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import settings
from app.database import AsyncSessionLocal, sync_engine, sync_session
from app.database import engine as async_engine
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreatorAgentEvent,
    CreatorAgentSession,
    CreatorPlanningAttempt,
    Persona,
    PlanItem,
    User,
)
from app.routes import creation_threads as routes
from app.routes import creator_agent as creator_agent_routes
from app.routes.creation_threads import MessageBody, message_thread
from app.tasks import creator_preparation as prepare_task

_database_name = make_url(settings.database_url).database or ""
if not _database_name.endswith("_test"):
    pytest.skip(
        f"refusing to write creation-thread integration fixtures to {_database_name!r}",
        allow_module_level=True,
    )
try:
    with sync_engine.connect() as _probe:
        _probe.execute(text("SELECT preparation FROM creator_agent_sessions LIMIT 0"))
except (OperationalError, OSError) as exc:
    pytest.skip(f"test PostgreSQL unavailable: {exc!r}", allow_module_level=True)

BARRIER_TIMEOUT_S = 15.0
DEADLOCK = "40P01"
_PROMPT = "Make a calm travel edit with the coast clip first"


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


def _wait_until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + BARRIER_TIMEOUT_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"barrier timed out waiting for {what}")
        time.sleep(0.01)


def _pg_flag(sql: str, pid: int | None) -> bool:
    if pid is None:
        return False
    with sync_engine.connect() as monitor:
        return bool(monitor.execute(text(sql), {"pid": pid}).scalar())


def _waiting_on_lock(pid: int | None) -> bool:
    return _pg_flag(
        "SELECT coalesce(bool_or(wait_event_type = 'Lock'), false) "
        "FROM pg_stat_activity WHERE pid = :pid",
        pid,
    )


def _someone_blocked_by(pid: int | None) -> bool:
    return _pg_flag(
        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
        "WHERE CAST(:pid AS integer) = ANY(pg_blocking_pids(pid)))",
        pid,
    )


def _sqlstate(exc: BaseException | None) -> str | None:
    while exc is not None:
        code = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
        if code:
            return str(code)
        if "deadlock detected" in str(exc):
            return DEADLOCK
        exc = getattr(exc, "orig", None) or exc.__cause__
    return None


@pytest.fixture()
def chat_ready_for_first_prompt(request: pytest.FixtureRequest):
    """A v1 chat with a format and one attached clip, before its first prompt."""

    # pytest-asyncio gives each async test its own loop; drop pooled asyncpg
    # connections created on an earlier loop (same pattern as the delete test).
    request.addfinalizer(lambda: async_engine.sync_engine.dispose(close=False))
    user_id, plan_id, item_id, thread_id = (uuid.uuid4() for _ in range(4))
    clip_path = f"users/{user_id}/creation-threads/{thread_id}/media/clip-1.mp4"
    with sync_session() as db:
        db.add(User(id=user_id, email=f"thread-link-{user_id}@example.test"))
        db.flush()
        persona = Persona(
            user_id=user_id,
            persona_status="edited",
            questionnaire={},
            persona={"summary": "test creator"},
            idea_seeds=[],
        )
        db.add(persona)
        db.flush()
        db.add(
            ContentPlan(
                id=plan_id,
                user_id=user_id,
                persona_id=persona.id,
                plan_status="edited",
                ownership_epoch=0,
            )
        )
        db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan_id,
                position=1,
                idea="Coast trip",
                edit_format="montage",
                montage_preset="classic",
                audio_mode="kria",
                content_mode="existing_footage",
                item_status="awaiting_clips",
                user_edited=True,
                clip_gcs_paths=[clip_path],
                clip_assignments=[
                    {
                        "media_id": "clip-1",
                        "gcs_path": clip_path,
                        "kind": "video",
                        "shot_id": None,
                        "storage_generation": "7",
                        "duration_s": 4.0,
                        "has_audio": True,
                        "manifest_identity": "clip-1",
                    }
                ],
            )
        )
        db.flush()
        db.add(
            CreationThread(
                id=thread_id,
                creator_id=user_id,
                runtime_version=1,
                title="Coast trip",
                content_plan_id=plan_id,
                active_plan_item_id=item_id,
                state={
                    "edit_format": "montage",
                    "media": [{"media_id": "clip-1", "kind": "video"}],
                    "media_count": 1,
                },
            )
        )
        db.commit()
    yield user_id, plan_id, item_id, thread_id
    with sync_session() as db:
        db.execute(text("DELETE FROM creation_threads WHERE creator_id = :u"), {"u": user_id})
        db.execute(text("DELETE FROM creator_agent_sessions WHERE creator_id = :u"), {"u": user_id})
        db.execute(text("DELETE FROM content_plans WHERE user_id = :u"), {"u": user_id})
        db.execute(text("DELETE FROM personas WHERE user_id = :u"), {"u": user_id})
        db.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        db.commit()


async def test_first_prompt_links_session_without_deadlocking_preparation_worker(
    chat_ready_for_first_prompt, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker queues behind a link that already holds its key-share locks."""

    race = await _race_first_prompt_with_preparation_worker(
        chat_ready_for_first_prompt, monkeypatch, worker_first=False
    )

    assert race.deadlocks == [], f"lock-order deadlock between link and worker: {race.deadlocks}"
    assert race.api_error is None, f"message_thread failed: {race.api_error!r}"
    _assert_linked_once(chat_ready_for_first_prompt, race)


async def test_first_prompt_link_waits_for_a_worker_already_holding_the_plan(
    chat_ready_for_first_prompt, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production interleaving: the worker holds Plan + PlanItem first.

    The link must queue behind it before it holds anything the worker still
    needs (the session), instead of key-sharing the session and then waiting
    on the item.
    """

    race = await _race_first_prompt_with_preparation_worker(
        chat_ready_for_first_prompt, monkeypatch, worker_first=True
    )

    assert race.deadlocks == [], f"lock-order deadlock between link and worker: {race.deadlocks}"
    assert race.api_error is None, f"message_thread failed: {race.api_error!r}"
    assert race.link_attempts == 1
    _assert_linked_once(chat_ready_for_first_prompt, race)


async def test_first_prompt_link_retries_a_lost_lock_race_without_duplicates(
    chat_ready_for_first_prompt, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link attempt that still loses a real 40P01 is retried, idempotently.

    The first attempt skips the up-front key-shares (the origin/main order),
    so the production deadlock happens for real and the link is its victim;
    the retry must link the committed session and project each event once,
    without re-running the controller or re-enqueueing the worker.
    """

    race = await _race_first_prompt_with_preparation_worker(
        chat_ready_for_first_prompt,
        monkeypatch,
        worker_first=True,
        skip_key_shares_on_first_attempt=True,
    )

    assert len(race.deadlocks) == 1, race.deadlocks
    assert race.deadlocks[0].startswith("api victim"), race.deadlocks
    assert race.link_attempts == 2
    assert race.api_error is None, f"message_thread failed: {race.api_error!r}"
    _assert_linked_once(chat_ready_for_first_prompt, race)


async def test_lost_link_race_without_the_retry_orphans_the_message_until_its_replay(
    chat_ready_for_first_prompt, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the race above: with the retry disabled the send fails.

    This is production's B8D898EC: the controller committed the message and
    the session, the link was the deadlock victim, and the chat stayed
    unlinked.  The client's same-id replay must then link it exactly once.
    """

    user_id, _plan_id, _item_id, thread_id = chat_ready_for_first_prompt
    monkeypatch.setattr(routes, "_SESSION_LINK_ATTEMPTS", 1)
    race = await _race_first_prompt_with_preparation_worker(
        chat_ready_for_first_prompt,
        monkeypatch,
        worker_first=True,
        skip_key_shares_on_first_attempt=True,
    )

    assert len(race.deadlocks) == 1, race.deadlocks
    assert race.deadlocks[0].startswith("api victim"), race.deadlocks
    assert _sqlstate(race.api_error) == DEADLOCK, repr(race.api_error)
    assert _thread_projection(thread_id) == (None, ["user_message"])

    await _replay_first_prompt(user_id, thread_id)

    _assert_linked_once(chat_ready_for_first_prompt, race)


async def test_session_link_builds_on_the_thread_as_committed_now(
    chat_ready_for_first_prompt, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The link re-reads the thread instead of reusing the controller's copy.

    Between the Creator commit and the link another request (a poll) can
    project onto the thread.  Re-selecting without ``populate_existing`` kept
    the identity map's stale ``revision``/``state``, so the link wrote them
    back: the other request's state was lost and a revision was reissued.
    """

    user_id, _plan_id, _item_id, thread_id = chat_ready_for_first_prompt
    await async_engine.dispose()
    monkeypatch.setattr(settings, "creator_clip_preparation_enabled", True)
    monkeypatch.setattr(
        prepare_task.prepare_creator_clips, "apply_async", lambda *args, **kwargs: None
    )
    real_lock_thread = routes._lock_thread_for_session_link
    concurrent_revision: list[int] = []

    async def _after_a_concurrent_projection(db, thread_id_, owner_id, session_id):  # noqa: ANN001
        with sync_session() as other:
            row = other.get(CreationThread, thread_id, with_for_update=True)
            row.state = {**row.state, "projected_elsewhere": True}
            row.revision += 1
            concurrent_revision.append(row.revision)
            other.commit()
        return await real_lock_thread(db, thread_id_, owner_id, session_id)

    monkeypatch.setattr(routes, "_lock_thread_for_session_link", _after_a_concurrent_projection)

    try:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            await message_thread(
                request=_request(),
                thread_id=str(thread_id),
                body=MessageBody(
                    message=_PROMPT,
                    client_event_id=f"first-prompt-{thread_id}",
                    expected_revision=0,
                ),
                user=user,
                db=db,
                native_client=True,
            )
    finally:
        await async_engine.dispose()

    assert concurrent_revision, "the link never ran"
    with sync_session() as db:
        thread_row = db.get(CreationThread, thread_id)
        acknowledgement = db.execute(
            select(CreationThreadEvent).where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.event_type == "status_update",
            )
        ).scalar_one()
    assert thread_row.state.get("projected_elsewhere") is True
    # The link's first append follows the concurrent revision; it never reissues it.
    assert acknowledgement.revision == concurrent_revision[0] + 1


class _Race(NamedTuple):
    api_error: DBAPIError | None
    deadlocks: list[str]
    link_attempts: int
    workers: list[threading.Thread]  # one per preparation publish, also later ones
    claims: list[object]


def _assert_linked_once(chat_ready_for_first_prompt, race: _Race) -> None:  # noqa: ANN001
    user_id, _plan_id, _item_id, thread_id = chat_ready_for_first_prompt
    with sync_session() as db:
        session_row = db.execute(
            select(CreatorAgentSession).where(CreatorAgentSession.creator_id == user_id)
        ).scalar_one()
        attempt = db.execute(
            select(CreatorPlanningAttempt).where(
                CreatorPlanningAttempt.session_id == session_row.id
            )
        ).scalar_one()
    pointer, event_types = _thread_projection(thread_id)
    # The chat is linked to the session it started and shows the durable
    # preparation acknowledgement, and the worker still claimed its attempt.
    assert pointer == session_row.id
    assert event_types[0] == "user_message"
    # Exactly once, also after a retried link: no duplicate message/receipt.
    assert event_types.count("user_message") == 1
    assert event_types.count("status_update") == 1
    assert len(race.workers) == 1, "the link must not re-run the controller or re-enqueue"
    assert race.claims and race.claims[0] is not None
    assert attempt.status == "running"


async def _race_first_prompt_with_preparation_worker(
    chat_ready_for_first_prompt,
    monkeypatch: pytest.MonkeyPatch,
    *,
    worker_first: bool,
    skip_key_shares_on_first_attempt: bool = False,
) -> _Race:
    user_id, _plan_id, _item_id, thread_id = chat_ready_for_first_prompt
    await async_engine.dispose()
    monkeypatch.setattr(settings, "creator_clip_preparation_enabled", True)

    # The worker gets its own one-connection engine so its backend pid is
    # stable across the claim's sessions (and its retries).
    worker_engine = create_engine(sync_engine.url, pool_size=1, max_overflow=0)
    monkeypatch.setattr(
        prepare_task, "sync_session", lambda: Session(worker_engine, expire_on_commit=False)
    )

    go = threading.Event()  # API reached the post-controller link section
    worker_at_session_lock = threading.Event()  # worker holds plan + item locks
    api_done = threading.Event()
    worker_pid: list[int] = []
    claims: list[object] = []
    deadlocks: list[str] = []
    worker_threads: list[threading.Thread] = []

    def _record_error(side: str):
        def listener(ctx) -> None:
            if _sqlstate(ctx.original_exception) == DEADLOCK:
                statement = " ".join(str(ctx.statement or "").split())[:90]
                deadlocks.append(f"{side} victim during {statement!r}")

        return listener

    api_listener = _record_error("api")
    worker_listener = _record_error("worker")
    event.listen(async_engine.sync_engine, "handle_error", api_listener)
    event.listen(worker_engine, "handle_error", worker_listener)

    session_lock_seen = False

    def _pause_before_session_lock(conn, cursor, statement, parameters, context, executemany):
        # Worker `_locked`: Plan FOR UPDATE -> PlanItem FOR UPDATE -> [here]
        # CreatorAgentSession FOR UPDATE.  Hold the plan/item locks until the
        # API is blocked behind them (or finished), then request the session.
        nonlocal session_lock_seen
        if session_lock_seen or "FROM creator_agent_sessions" not in statement:
            return
        if "FOR UPDATE" not in statement:
            return
        session_lock_seen = True
        worker_at_session_lock.set()
        _wait_until(
            lambda: api_done.is_set() or _someone_blocked_by(worker_pid[0]),
            "the API request to block on the worker's plan/item locks, or finish",
        )

    event.listen(worker_engine, "before_cursor_execute", _pause_before_session_lock)

    def _run_worker(attempt_id: str) -> None:
        go.wait(BARRIER_TIMEOUT_S)
        with worker_engine.connect() as conn:
            worker_pid.append(int(conn.execute(text("SELECT pg_backend_pid()")).scalar_one()))
        claims.append(prepare_task._claim(uuid.UUID(attempt_id)))

    def _publish(*, args, task_id):  # noqa: ANN001, ARG001 - Celery signature
        thread = threading.Thread(target=_run_worker, args=(args[0],), daemon=True)
        worker_threads.append(thread)
        thread.start()

    monkeypatch.setattr(prepare_task.prepare_creator_clips, "apply_async", _publish)

    real_sync_agent = routes._sync_agent
    real_lock_thread = routes._lock_thread_for_session_link
    gate_passed = False

    async def _gated_sync_agent(db, thread):  # noqa: ANN001
        # Post-controller section: the Creator commit is done and the worker
        # is enqueued.  Let the worker lock Plan -> PlanItem now (or queue
        # behind whatever this request already locked) before projecting.
        nonlocal gate_passed
        if not gate_passed:
            gate_passed = True
            go.set()
            await asyncio.to_thread(
                _wait_until,
                lambda: (
                    worker_at_session_lock.is_set()
                    or _waiting_on_lock(worker_pid[0] if worker_pid else None)
                ),
                "the worker to hold Plan/PlanItem or queue behind this request",
            )
        return await real_sync_agent(db, thread)

    real_key_share_parents = routes._key_share_thread_parents
    link_attempts = 0

    async def _skip_key_share(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        return None

    async def _gated_lock_thread(db, thread_id, owner_id, session_id):  # noqa: ANN001
        # Before the link takes any lock: the worker holds Plan -> PlanItem
        # and is about to request the session.
        nonlocal gate_passed, link_attempts
        link_attempts += 1
        if not gate_passed:
            gate_passed = True
            go.set()
            await asyncio.to_thread(
                _wait_until,
                worker_at_session_lock.is_set,
                "the worker to hold Plan/PlanItem before the link starts",
            )
        if skip_key_shares_on_first_attempt:
            monkeypatch.setattr(
                routes,
                "_key_share_thread_parents",
                _skip_key_share if link_attempts == 1 else real_key_share_parents,
            )
        return await real_lock_thread(db, thread_id, owner_id, session_id)

    if worker_first:
        monkeypatch.setattr(routes, "_lock_thread_for_session_link", _gated_lock_thread)
    else:
        monkeypatch.setattr(routes, "_sync_agent", _gated_sync_agent)

    api_error: DBAPIError | None = None
    try:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            assert user is not None
            try:
                await message_thread(
                    request=_request(),
                    thread_id=str(thread_id),
                    body=MessageBody(
                        message=_PROMPT,
                        client_event_id=f"first-prompt-{thread_id}",
                        expected_revision=0,
                    ),
                    user=user,
                    db=db,
                    native_client=True,
                )
            except DBAPIError as exc:
                api_error = exc
            finally:
                api_done.set()
        for thread in worker_threads:
            thread.join(BARRIER_TIMEOUT_S)
            assert not thread.is_alive(), "preparation worker did not finish"
    finally:
        event.remove(async_engine.sync_engine, "handle_error", api_listener)
        event.remove(worker_engine, "handle_error", worker_listener)
        event.remove(worker_engine, "before_cursor_execute", _pause_before_session_lock)
        worker_engine.dispose()
        await async_engine.dispose()

    assert gate_passed, "message never reached the post-controller link section"
    assert worker_threads, "maybe_prepare did not publish the preparation worker"
    return _Race(api_error, deadlocks, link_attempts, worker_threads, claims)


def _leave_unlinked_start(
    user_id: uuid.UUID,
    item_id: uuid.UUID,
    thread_id: uuid.UUID,
    *,
    session_client_event_id: str,
) -> uuid.UUID:
    """Commit the state a lost post-Creator link leaves behind; return the session.

    The thread's ``user_message`` (client id ``first-prompt-<thread>``) and the
    Creator session with its reply are durable, but the thread still points at
    no session.  ``session_client_event_id`` is the id the session's own user
    message carries: the thread's id when the thread started it.
    """

    session_id = uuid.uuid4()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.revision = 1
        db.add(
            CreationThreadEvent(
                thread_id=thread_id,
                sequence=0,
                revision=1,
                role="user",
                event_type="user_message",
                content=_PROMPT,
                client_event_id=f"first-prompt-{thread_id}",
            )
        )
        db.add(
            CreatorAgentSession(
                id=session_id,
                creator_id=user_id,
                plan_item_id=item_id,
                status="awaiting_confirmation",
                revision=3,
                ownership_epoch=0,
            )
        )
        db.flush()
        db.add(
            CreatorAgentEvent(
                session_id=session_id,
                sequence=0,
                revision=1,
                role="user",
                event_type="user_message",
                payload={"message": _PROMPT},
                client_event_id=session_client_event_id,
            )
        )
        db.add(
            CreatorAgentEvent(
                session_id=session_id,
                sequence=1,
                revision=3,
                role="assistant",
                event_type="assistant_proposal",
                payload={"message": "Here is a calm coastal cut."},
            )
        )
        db.commit()
    return session_id


def _thread_projection(thread_id: uuid.UUID) -> tuple[uuid.UUID | None, list[str]]:
    with sync_session() as db:
        pointer = db.get(CreationThread, thread_id).active_creator_agent_session_id
        projected = [
            row.event_type
            for row in db.execute(
                select(CreationThreadEvent)
                .where(CreationThreadEvent.thread_id == thread_id)
                .order_by(CreationThreadEvent.sequence)
            ).scalars()
        ]
    return pointer, projected


async def _replay_first_prompt(user_id: uuid.UUID, thread_id: uuid.UUID):  # noqa: ANN202
    try:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            return await message_thread(
                request=_request(),
                thread_id=str(thread_id),
                body=MessageBody(
                    message=_PROMPT,
                    client_event_id=f"first-prompt-{thread_id}",
                    expected_revision=0,
                ),
                user=user,
                db=db,
                native_client=True,
            )
    finally:
        await async_engine.dispose()


@pytest.mark.parametrize("previous", [None, "failed"], ids=["unlinked", "failed-session"])
async def test_replayed_prompt_after_failed_link_returns_and_relinks_once(
    chat_ready_for_first_prompt, previous: str | None
) -> None:
    """The client's idempotent replay of that failed prompt must heal the chat.

    The duplicate branch rolled back and reloaded with ``user.id``; the
    rollback had expired ``user`` (the route and ``get_current_user`` share one
    session), so on origin/main the replay died with MissingGreenlet (prod
    10:39 on attach_media, same branch shape) and the thread stayed orphaned.
    A message after a failed session starts a fresh one, so a lost link there
    leaves the thread on the failed session instead of on none.
    """

    user_id, _plan_id, item_id, thread_id = chat_ready_for_first_prompt
    await async_engine.dispose()
    if previous is not None:
        previous_id = uuid.uuid4()
        with sync_session() as db:
            db.add(
                CreatorAgentSession(
                    id=previous_id,
                    creator_id=user_id,
                    plan_item_id=item_id,
                    status=previous,
                    ownership_epoch=0,
                )
            )
            db.flush()
            db.get(CreationThread, thread_id).active_creator_agent_session_id = previous_id
            db.commit()
    session_id = _leave_unlinked_start(
        user_id, item_id, thread_id, session_client_event_id=f"first-prompt-{thread_id}"
    )

    out = await _replay_first_prompt(user_id, thread_id)

    assert str(out.id) == str(thread_id)
    pointer, projected = _thread_projection(thread_id)
    assert pointer == session_id
    assert projected.count("agent_assistant_proposal") == 1
    # Linked once: a further replay changes nothing.
    await _replay_first_prompt(user_id, thread_id)
    assert _thread_projection(thread_id) == (pointer, projected)


async def test_replay_heal_waits_on_a_busy_session_without_holding_the_user_lock(
    chat_ready_for_first_prompt,
) -> None:
    """The heal must not wait on the session while it holds the user FOR UPDATE.

    A Creator turn holds its session FOR UPDATE and appends events; each
    ``append_event`` bumps ``revision``, so its second UPDATE of the session row
    re-checks the ``users`` foreign key (FOR KEY SHARE).  The replay's duplicate
    check runs under ``message_thread``'s user FOR UPDATE.  Healing under that
    lock waits on the session's key-share while the turn waits on the user: a
    deadlock.  The heal therefore links under the link's own lock order (user
    FOR KEY SHARE), which the turn's foreign-key check passes through.
    """

    user_id, _plan_id, item_id, thread_id = chat_ready_for_first_prompt
    await async_engine.dispose()
    session_id = _leave_unlinked_start(
        user_id, item_id, thread_id, session_client_event_id=f"first-prompt-{thread_id}"
    )
    turn_engine = create_engine(sync_engine.url, pool_size=1, max_overflow=0)
    holding_session = threading.Event()
    replay_done = threading.Event()
    turn_errors: list[BaseException] = []

    def _creator_turn_writing_the_session() -> None:
        try:
            with turn_engine.begin() as conn:
                pid = int(conn.execute(text("SELECT pg_backend_pid()")).scalar_one())
                conn.execute(
                    text("SELECT id FROM creator_agent_sessions WHERE id = :s FOR UPDATE"),
                    {"s": session_id},
                )
                holding_session.set()
                _wait_until(
                    lambda: replay_done.is_set() or _someone_blocked_by(pid),
                    "the replay to queue behind the session lock, or finish",
                )
                for _ in range(2):  # two append_event revision bumps
                    conn.execute(
                        text(
                            "UPDATE creator_agent_sessions SET revision = revision + 1 "
                            "WHERE id = :s"
                        ),
                        {"s": session_id},
                    )
        except BaseException as exc:  # noqa: BLE001 - asserted below
            turn_errors.append(exc)

    turn = threading.Thread(target=_creator_turn_writing_the_session, daemon=True)
    turn.start()
    try:
        assert holding_session.wait(BARRIER_TIMEOUT_S), "the Creator turn never locked"
        try:
            out = await _replay_first_prompt(user_id, thread_id)
        finally:
            replay_done.set()
            turn.join(BARRIER_TIMEOUT_S)
        assert not turn.is_alive(), "the Creator turn did not finish"
    finally:
        turn_engine.dispose()

    assert turn_errors == [], f"the Creator turn failed: {turn_errors!r}"
    assert str(out.id) == str(thread_id)
    pointer, projected = _thread_projection(thread_id)
    assert pointer == session_id
    assert projected.count("agent_assistant_proposal") == 1


async def test_replay_heal_rechecks_the_thread_under_the_link_locks(
    chat_ready_for_first_prompt, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session linked after the replay's own check is never overwritten.

    The heal releases the replay's locks before it links, so the thread can
    move on in between: here the replayed start fails and a newer session is
    linked.  The link must re-check and keep that session.
    """

    user_id, _plan_id, item_id, thread_id = chat_ready_for_first_prompt
    await async_engine.dispose()
    started_id = _leave_unlinked_start(
        user_id, item_id, thread_id, session_client_event_id=f"first-prompt-{thread_id}"
    )
    newer_id = uuid.uuid4()
    real_lock_thread = routes._lock_thread_for_session_link

    async def _after_a_newer_session_was_linked(db, thread_id_, owner_id, session_id):  # noqa: ANN001
        if newer_id not in _linked:
            with sync_session() as other:
                other.get(CreatorAgentSession, started_id).status = "failed"
                other.flush()
                other.add(
                    CreatorAgentSession(
                        id=newer_id,
                        creator_id=user_id,
                        plan_item_id=item_id,
                        status="briefing",
                        ownership_epoch=0,
                    )
                )
                other.flush()
                other.get(CreationThread, thread_id).active_creator_agent_session_id = newer_id
                other.commit()
            _linked.append(newer_id)
        return await real_lock_thread(db, thread_id_, owner_id, session_id)

    _linked: list[uuid.UUID] = []
    monkeypatch.setattr(routes, "_lock_thread_for_session_link", _after_a_newer_session_was_linked)

    out = await _replay_first_prompt(user_id, thread_id)

    assert _linked == [newer_id], "the heal never reached its link"
    assert str(out.id) == str(thread_id)
    assert _thread_projection(thread_id) == (newer_id, ["user_message"])


async def test_replay_never_links_a_session_this_message_did_not_start(
    chat_ready_for_first_prompt,
) -> None:
    """A session on the same item started from another surface stays unlinked."""

    user_id, _plan_id, item_id, thread_id = chat_ready_for_first_prompt
    await async_engine.dispose()
    _leave_unlinked_start(user_id, item_id, thread_id, session_client_event_id="web-plan-page")

    out = await _replay_first_prompt(user_id, thread_id)

    assert str(out.id) == str(thread_id)
    assert _thread_projection(thread_id) == (None, ["user_message"])


async def test_start_insert_race_keeps_the_chat_message_and_joins_the_winner(
    chat_ready_for_first_prompt, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost race on the active-session unique index must not drop the message.

    The start controller used to answer that IntegrityError with a full
    ``rollback()``: it discarded the chat's uncommitted ``user_message`` (same
    transaction) and expired ``user``, whose next read raised MissingGreenlet.
    """

    user_id, _plan_id, item_id, thread_id = chat_ready_for_first_prompt
    await async_engine.dispose()
    monkeypatch.setattr(settings, "creator_clip_preparation_enabled", True)
    monkeypatch.setattr(
        prepare_task.prepare_creator_clips, "apply_async", lambda *args, **kwargs: None
    )
    winner_id = uuid.uuid4()
    with sync_session() as db:
        db.add(
            CreatorAgentSession(
                id=winner_id,
                creator_id=user_id,
                plan_item_id=item_id,
                status="briefing",
                ownership_epoch=0,
            )
        )
        db.commit()
    real_latest_session = creator_agent_routes._latest_session
    hidden = False

    async def _latest_session_missing_the_winner(db, user_id, item_id, *, active_only=False):  # noqa: ANN001
        # The rival start commits after this request's read and before its
        # INSERT; replay that window deterministically.
        nonlocal hidden
        if active_only and not hidden:
            hidden = True
            return None
        return await real_latest_session(db, user_id, item_id, active_only=active_only)

    monkeypatch.setattr(creator_agent_routes, "_latest_session", _latest_session_missing_the_winner)

    try:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            await message_thread(
                request=_request(),
                thread_id=str(thread_id),
                body=MessageBody(
                    message=_PROMPT,
                    client_event_id=f"first-prompt-{thread_id}",
                    expected_revision=0,
                ),
                user=user,
                db=db,
                native_client=True,
            )
    finally:
        await async_engine.dispose()

    assert hidden, "the start controller never reached its INSERT"
    pointer, projected = _thread_projection(thread_id)
    assert pointer == winner_id
    assert projected[0] == "user_message"
    with sync_session() as db:
        sessions = db.execute(
            select(CreatorAgentSession.id).where(CreatorAgentSession.creator_id == user_id)
        ).scalars()
        winner_events = db.execute(
            select(CreatorAgentEvent.event_type).where(CreatorAgentEvent.session_id == winner_id)
        ).scalars()
        assert list(sessions) == [winner_id]
        assert "user_message" in list(winner_events)
