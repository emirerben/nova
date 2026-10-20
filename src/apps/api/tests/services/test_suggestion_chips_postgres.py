"""KRI-756 backend gate (c) as a regression test, on real Postgres.

The offered-chip ring is written by a GET. The ticket's invariants:

* the write is revision-neutral (no `thread.revision` bump, no event, `updated_at` untouched);
* it is idempotent (the same set_id is written once);
* the ring is bounded (last 5 sets);
* it never waits behind a turn that holds the thread row (FOR UPDATE SKIP LOCKED);
* it is hidden from the public thread state.

Skips unless DATABASE_URL names a ``*_test`` database that is reachable (same guard as
tests/tasks/test_reaper_jsonpath.py), so it runs in test-api CI and locally against a scratch
database but never against a dev database.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.models import CreationThread, CreationThreadEvent, User
from app.services import suggestion_chips as sc

if not (make_url(settings.database_url).database or "").endswith("_test"):
    pytest.skip("requires a *_test database", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
        _probe.execute(text("select 1 from creation_threads limit 1"))
except Exception as _exc:  # noqa: BLE001 - unreachable or unmigrated => skip, never fail CI
    pytest.skip(f"nova_test Postgres not ready: {type(_exc).__name__}", allow_module_level=True)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


def _set(n: int, *, state: str = "first_prompt", kinds=("default_edit", "captions")):
    messages = {"default_edit": "Suggest an edit.", "captions": "Add captions"}
    return sc.SuggestionSetOut(
        set_id=f"sg_{n:016d}",
        state=state,
        status="ok",
        anchor=sc.SuggestionAnchorOut(media_digest=f"digest-{n}"),
        chips=[
            sc.SuggestionChipOut(
                id=f"sc_{k}{n}",
                kind=k,
                label=k,
                message=messages[k],
                rank=i + 1,
                default_edit=i == 0,
                label_source="agent" if i else "template",
            )
            for i, k in enumerate(kinds)
        ],
    )


@pytest.fixture
def thread_id():
    """A committed thread with revision 3 and two events; removed afterwards."""
    user_id = uuid.uuid4()
    tid = uuid.uuid4()
    with sync_session() as s:
        s.add(User(id=user_id, email=f"{user_id}@test.local"))
        s.flush()
        s.add(
            CreationThread(
                id=tid,
                creator_id=user_id,
                runtime_version=2,
                revision=3,
                state={"intent": "keep me"},
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        s.flush()
        for seq in range(2):
            s.add(
                CreationThreadEvent(
                    thread_id=tid,
                    sequence=seq,
                    role="system",
                    event_type="status_update",
                    revision=seq + 1,
                )
            )
        s.commit()
    yield tid
    with sync_session() as s:
        s.execute(text("delete from users where id = :u"), {"u": user_id})
        s.commit()


def _snapshot(tid):
    with sync_session() as s:
        row = s.execute(
            select(CreationThread.revision, CreationThread.updated_at, CreationThread.state).where(
                CreationThread.id == tid
            )
        ).one()
        events = s.execute(
            select(func.count())
            .select_from(CreationThreadEvent)
            .where(CreationThreadEvent.thread_id == tid)
        ).scalar_one()
    return {"revision": row[0], "updated_at": row[1], "state": row[2], "events": events}


async def test_ring_write_is_revision_neutral_and_idempotent(thread_id):
    before = _snapshot(thread_id)
    async with AsyncSessionLocal() as db:
        wrote_first = await sc.persist_suggestion_set(
            db, thread_id, _set(1), media_digest="digest-1", protocol=2
        )
    async with AsyncSessionLocal() as db:
        wrote_again = await sc.persist_suggestion_set(
            db, thread_id, _set(1), media_digest="digest-1", protocol=2
        )
    after = _snapshot(thread_id)
    assert (wrote_first, wrote_again) == (True, False)
    assert after["revision"] == before["revision"] == 3, "a GET must never bump the revision"
    assert after["events"] == before["events"] == 2, "a GET must append no event"
    assert after["updated_at"] == before["updated_at"], "a GET must not reorder the project list"
    assert after["state"]["intent"] == "keep me", "the rest of thread.state survives the merge"
    ring = after["state"][sc.RING_KEY]
    assert [e["set_id"] for e in ring] == [_set(1).set_id]
    assert ring[0]["chips"][0] == {
        "id": "sc_default_edit1",
        "kind": "default_edit",
        "rank": 1,
        "message": "Suggest an edit.",
        "label_source": "template",
        "default_edit": True,
    }
    assert ring[0]["final"] is True and ring[0]["prompt_version"] == sc.PROMPT_VERSION
    assert ring[0]["media_digest"] == "digest-1"


async def test_a_pending_set_is_replaced_in_place_by_its_final_set(thread_id):
    """The pending default_edit-only set and the final set share a set_id: a tap on the
    pending chip is attributed, the final set replaces the entry, and a final entry is
    never rewritten (labels are frozen per digest)."""
    final = _set(1)
    pending = final.model_copy(
        update={"status": "pending", "chips": final.chips[:1], "wording": "pending"}
    )
    async with AsyncSessionLocal() as db:
        assert await sc.persist_suggestion_set(
            db, thread_id, pending, media_digest="digest-1", protocol=2
        )
    ring = _snapshot(thread_id)["state"][sc.RING_KEY]
    assert [e["final"] for e in ring] == [False] and len(ring[0]["chips"]) == 1
    async with AsyncSessionLocal() as db:
        assert await sc.persist_suggestion_set(
            db, thread_id, final, media_digest="digest-1", protocol=2
        )
    ring = _snapshot(thread_id)["state"][sc.RING_KEY]
    assert [e["final"] for e in ring] == [True] and len(ring[0]["chips"]) == 2
    async with AsyncSessionLocal() as db:
        assert not await sc.persist_suggestion_set(
            db, thread_id, pending, media_digest="digest-1", protocol=2
        )
        assert not await sc.persist_suggestion_set(
            db, thread_id, final, media_digest="digest-1", protocol=2
        )
    assert len(_snapshot(thread_id)["state"][sc.RING_KEY]) == 1


async def test_ring_is_bounded_to_the_last_five_sets(thread_id):
    for n in range(1, 8):
        async with AsyncSessionLocal() as db:
            assert await sc.persist_suggestion_set(
                db, thread_id, _set(n), media_digest=f"digest-{n}", protocol=2
            )
    ring = _snapshot(thread_id)["state"][sc.RING_KEY]
    assert [e["set_id"] for e in ring] == [_set(n).set_id for n in range(3, 8)]


async def test_an_empty_or_unavailable_set_is_never_persisted(thread_id):
    for out in (sc.unavailable_set(), sc.SuggestionSetOut(set_id="sg_x", status="ok", chips=[])):
        async with AsyncSessionLocal() as db:
            assert not await sc.persist_suggestion_set(
                db, thread_id, out, media_digest="d", protocol=2
            )
    assert sc.RING_KEY not in _snapshot(thread_id)["state"]


async def test_persist_never_waits_behind_a_turn_that_holds_the_thread(thread_id):
    holder = AsyncSessionLocal()
    try:
        await holder.execute(
            select(CreationThread.id).where(CreationThread.id == thread_id).with_for_update()
        )
        async with AsyncSessionLocal() as db:
            wrote = await asyncio.wait_for(
                sc.persist_suggestion_set(db, thread_id, _set(1), media_digest="d", protocol=2),
                timeout=5,
            )
        assert wrote is False, "a locked thread skips the write; the next poll records it"
    finally:
        await holder.rollback()
        await holder.close()
    assert sc.RING_KEY not in _snapshot(thread_id)["state"]


async def test_a_chip_message_matches_the_ring_read_back_from_the_row(thread_id):
    """Persist a set, read the row back, match a typed message against the stored ring."""
    await _persist(thread_id, _set(1))
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, thread_id)
        hit = sc.match_offered_suggestion(thread, "add captions")
    assert hit == {
        "set_id": _set(1).set_id,
        "chip_id": "sc_captions1",
        "kind": "captions",
        "rank": 2,
        "state": "first_prompt",
        "label_source": "agent",
        "prompt_version": sc.PROMPT_VERSION,
        "default_edit": False,
    }


async def _persist(tid, out):
    async with AsyncSessionLocal() as db:
        assert await sc.persist_suggestion_set(
            db, tid, out, media_digest=out.anchor.media_digest, protocol=2
        )


async def test_the_public_thread_projection_hides_the_ring(thread_id):
    from app.routes.creation_threads import _response

    await _persist(thread_id, _set(1))
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, thread_id)
        assert sc.RING_KEY in thread.state
        public = await _response(db, thread)
    assert sc.RING_KEY not in public.state


def test_funnel_loader_reads_the_ring_and_the_stamped_events(thread_id):
    """scripts/suggestion_chip_funnel.py database mode, read-only, over real rows."""
    from scripts import suggestion_chip_funnel as funnel

    stamp = {
        "set_id": _set(1).set_id,
        "chip_id": "sc_captions1",
        "kind": "captions",
        "rank": 2,
        "state": "first_prompt",
        "label_source": "agent",
        "prompt_version": sc.PROMPT_VERSION,
        "default_edit": False,
    }
    with sync_session() as s:
        row = s.get(CreationThread, thread_id)
        row.state = {**row.state, sc.RING_KEY: [_ring_entry(_set(1))]}
        s.add(
            CreationThreadEvent(
                thread_id=thread_id,
                sequence=10,
                role="user",
                event_type="user_message",
                content="Add captions",
                payload={"suggestion": stamp},
                revision=10,
            )
        )
        s.add(
            CreationThreadEvent(
                thread_id=thread_id,
                sequence=11,
                role="assistant",
                event_type="draft_applied",
                revision=11,
            )
        )
        s.commit()
    records = [
        r for r in funnel.load_threads_from_db(days=1, limit=50) if r.thread_id == str(thread_id)
    ]
    assert len(records) == 1
    result = funnel.derive_funnel(records)
    cell = result.cells[("first_prompt", "captions", 2)]
    assert (cell.shown, cell.tapped, cell.completed, cell.kept) == (1, 1, 1, 1)


def _ring_entry(out):
    return {
        "set_id": out.set_id,
        "state": out.state,
        "digest": out.anchor.media_digest,
        "media_digest": out.anchor.media_digest,
        "created_at": "2026-10-19T00:00:00+00:00",
        "chips": [
            {
                "id": c.id,
                "kind": c.kind,
                "rank": c.rank,
                "message": c.message,
                "label_source": c.label_source,
                "default_edit": c.default_edit,
            }
            for c in out.chips
        ],
    }
