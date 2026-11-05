"""The freshness ring writes on real Postgres: dismiss, fold, attach, eviction, locks.

The ring lives in ``thread.state`` and is written from a GET and from the dismiss route while
creator turns hold the thread row. The invariants (the pure logic is in
``test_suggestion_freshness.py``):

* every write is revision-neutral: no ``thread.revision`` bump, no event, ``updated_at`` pinned;
* every write is ``FOR UPDATE SKIP LOCKED`` and never waits behind a turn: a locked row is reported
  ``busy`` (dismiss) or skipped (the GET's writes) and nothing is written;
* the ring keeps 5 sets, and what the creator dismissed or applied survives the eviction;
* a foreign thread is the usual 404 and writes nothing; an unknown id writes nothing;
* two dismisses at once end in one stored dismissal.

Skips unless DATABASE_URL names a reachable migrated ``*_test`` database, so it runs against the
scratch database from ``scripts/dev/test-db.sh`` and never against a dev database. Where
``NOVA_REQUIRE_POSTGRES_TESTS`` is set (the api shards, which provide ``nova_test``) that is a
collection failure, not a skip, so a broken CI database can never read as green
(``test_suggestion_gates_ci_guard.py``).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.models import CreationThread, CreationThreadEvent, User
from app.services import suggestion_chips as sc
from app.services import suggestion_serve as serve
from app.services.suggestions.models import (
    ChipAnchorOut,
    EditorOpsAction,
    Suggestion,
    SuggestionOut,
)
from tests.postgres_required import postgres_required


def _database_problem() -> str | None:
    name = make_url(settings.database_url).database or ""
    if not name.endswith("_test"):
        return f"refusing to use non-test database {name!r}"
    try:
        with sync_session() as probe:
            probe.execute(text("select 1 from creation_threads limit 1"))
    except Exception as exc:  # noqa: BLE001 - unreachable or unmigrated: the fixtures cannot run
        return f"nova_test Postgres not reachable or not migrated ({type(exc).__name__})"
    return None


_PROBLEM = _database_problem()
if _PROBLEM and postgres_required():
    pytest.fail(f"{_PROBLEM}: the freshness ring fixtures must run in CI", pytrace=False)
if _PROBLEM:
    pytest.skip(_PROBLEM, allow_module_level=True)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


@pytest.fixture
def owner():
    """A committed thread (revision 3, two events) and its owner; removed afterwards."""
    user_id, thread_id = uuid.uuid4(), uuid.uuid4()
    with sync_session() as s:
        s.add(User(id=user_id, email=f"{user_id}@test.local"))
        s.flush()
        s.add(
            CreationThread(
                id=thread_id,
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
                    thread_id=thread_id,
                    sequence=seq,
                    role="system",
                    event_type="status_update",
                    revision=seq + 1,
                )
            )
        s.commit()
    yield SimpleNamespace(user_id=user_id, thread_id=thread_id)
    with sync_session() as s:
        s.execute(text("delete from users where id = :u"), {"u": user_id})
        s.commit()


def _snapshot(thread_id):
    with sync_session() as s:
        row = s.execute(
            select(CreationThread.revision, CreationThread.updated_at, CreationThread.state).where(
                CreationThread.id == thread_id
            )
        ).one()
        events = s.execute(
            select(func.count())
            .select_from(CreationThreadEvent)
            .where(CreationThreadEvent.thread_id == thread_id)
        ).scalar_one()
    return {"revision": row[0], "updated_at": row[1], "state": row[2], "events": events}


def _write_state(thread_id, **state):
    with sync_session() as s:
        row = s.get(CreationThread, thread_id)
        row.state = {**row.state, **state}
        s.commit()


MUTE_3 = Suggestion(
    id="ss_mute3",
    kind="mute_clip",
    title="Mute clip 3",
    prompt="Mute clip 3, it has wind noise",
    rank=1,
    anchor=ChipAnchorOut(media_id="m3", clip_ordinal=3),
    action=EditorOpsAction(ops=[{"op": "set_mix", "level": 0.5}], base_digest="d1"),
)
CAPTIONS = Suggestion(
    id="ss_caps",
    kind="captions",
    title="Add captions",
    prompt="Add captions",
    rank=2,
    action=EditorOpsAction(ops=[{"op": "set_caption_meta", "enabled": True}], base_digest="d1"),
)


def _entry(n, cards=(MUTE_3, CAPTIONS), **extra):
    return {
        "set_id": f"sg_{n:016d}",
        "state": "ready",
        "digest": f"digest-{n}",
        "media_digest": "m",
        "final": True,
        "chips": [],
        "suggestions": [c.model_dump(mode="json") for c in cards],
        **extra,
    }


def _served(n, cards=(MUTE_3, CAPTIONS)):
    wire = [
        SuggestionOut(
            id=c.id,
            kind=c.kind,
            title=c.title,
            prompt=c.prompt,
            rank=i + 1,
            anchor=c.anchor,
            action_type="editor_ops",
        )
        for i, c in enumerate(cards)
    ]
    out = sc.SuggestionSetOut(
        set_id=f"sg_{n:016d}",
        state="ready",
        status="ok",
        anchor=sc.SuggestionAnchorOut(media_digest="m"),
        chips=[
            sc.SuggestionChipOut(
                id=f"sc_{n}", kind="captions", label="Add captions", message="Add captions", rank=1
            )
        ],
        suggestions=wire,
    )
    served = serve.Served(out=out, media_digest="m", input_digest=f"digest-{n}")
    served.unfiltered = out
    return served


# ── Dismiss ──────────────────────────────────────────────────────────────────────────


async def test_dismiss_is_revision_neutral_idempotent_and_keeps_the_rest_of_the_state(owner):
    _write_state(owner.thread_id, **{sc.RING_KEY: [_entry(1)]})
    before = _snapshot(owner.thread_id)
    async with AsyncSessionLocal() as db:
        first = await serve.dismiss_suggestion(
            db, owner.thread_id, "sg_" + "0" * 15 + "1", "ss_mute3"
        )
    async with AsyncSessionLocal() as db:
        again = await serve.dismiss_suggestion(
            db, owner.thread_id, "sg_" + "0" * 15 + "1", "ss_mute3"
        )
    after = _snapshot(owner.thread_id)
    assert (first, again) == ("dismissed", "already_dismissed")
    assert after["revision"] == before["revision"] == 3
    assert after["events"] == before["events"] == 2
    assert after["updated_at"] == before["updated_at"], "a dismiss must not reorder the projects"
    assert after["state"]["intent"] == "keep me"
    fp = serve.fingerprint("mute_clip", {"media_id": "m3", "clip_ordinal": 3})
    assert after["state"][serve.SUPPRESSED_KEY] == [fp]
    assert after["state"][sc.RING_KEY][0][serve.DISMISSED_FIELD] == {"ss_mute3": fp}


async def test_dismiss_of_an_unknown_id_or_an_empty_ring_writes_nothing(owner):
    before = _snapshot(owner.thread_id)
    async with AsyncSessionLocal() as db:
        assert await serve.dismiss_suggestion(db, owner.thread_id, "sg_1", "ss_x") == "unknown"
    _write_state(owner.thread_id, **{sc.RING_KEY: [_entry(1)]})
    mid = _snapshot(owner.thread_id)
    async with AsyncSessionLocal() as db:
        assert await serve.dismiss_suggestion(db, owner.thread_id, "sg_1", "ss_x") == "unknown"
    assert _snapshot(owner.thread_id) == mid
    assert before["state"] == {"intent": "keep me"}


async def test_dismiss_after_the_set_rolled_out_of_the_ring_is_unknown_and_writes_nothing(owner):
    _write_state(owner.thread_id, **{sc.RING_KEY: [_entry(n, (CAPTIONS,)) for n in range(2, 7)]})
    before = _snapshot(owner.thread_id)
    async with AsyncSessionLocal() as db:
        out = await serve.dismiss_suggestion(
            db, owner.thread_id, "sg_" + "0" * 15 + "1", "ss_mute3"
        )
    assert out == "unknown" and _snapshot(owner.thread_id) == before


async def test_dismiss_finds_the_card_in_the_newer_set_when_the_client_names_the_old_one(owner):
    _write_state(owner.thread_id, **{sc.RING_KEY: [_entry(2)]})
    async with AsyncSessionLocal() as db:
        out = await serve.dismiss_suggestion(
            db, owner.thread_id, "sg_" + "0" * 15 + "1", "ss_mute3"
        )
    assert out == "dismissed"


async def test_dismiss_never_waits_behind_a_turn_that_holds_the_thread(owner):
    _write_state(owner.thread_id, **{sc.RING_KEY: [_entry(1)]})
    before = _snapshot(owner.thread_id)
    holder = AsyncSessionLocal()
    try:
        await holder.execute(
            select(CreationThread.id).where(CreationThread.id == owner.thread_id).with_for_update()
        )
        async with AsyncSessionLocal() as db:
            out = await asyncio.wait_for(
                serve.dismiss_suggestion(db, owner.thread_id, "sg_" + "0" * 15 + "1", "ss_mute3"),
                timeout=5,
            )
    finally:
        await holder.rollback()
        await holder.close()
    assert out == "busy", "a locked thread is reported, never waited on, never half-written"
    assert _snapshot(owner.thread_id) == before
    async with AsyncSessionLocal() as db:  # the app sends it again once the turn is done
        assert (
            await serve.dismiss_suggestion(db, owner.thread_id, "sg_" + "0" * 15 + "1", "ss_mute3")
            == "dismissed"
        )


async def test_two_dismisses_at_once_store_one_dismissal(owner):
    _write_state(owner.thread_id, **{sc.RING_KEY: [_entry(1)]})

    async def one():
        async with AsyncSessionLocal() as db:
            return await serve.dismiss_suggestion(
                db, owner.thread_id, "sg_" + "0" * 15 + "1", "ss_mute3"
            )

    outcomes = await asyncio.gather(one(), one())
    assert sorted(outcomes) in (["already_dismissed", "dismissed"], ["busy", "dismissed"])
    state = _snapshot(owner.thread_id)["state"]
    assert len(state[serve.SUPPRESSED_KEY]) == 1
    assert list(state[sc.RING_KEY][0][serve.DISMISSED_FIELD]) == ["ss_mute3"]


# ── The route: flags, ownership ──────────────────────────────────────────────────────


def _on(monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", True)
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", True)


async def _dismiss_route(user_id, thread_id, set_id="sg_1", card_id="ss_mute3"):
    from starlette.responses import Response

    from app.kria.api_schemas import SuggestionTapIn
    from app.routes import creation_threads as ct

    response = Response()
    async with AsyncSessionLocal() as db:
        out = await ct.dismiss_thread_suggestion(
            str(thread_id),
            SuggestionTapIn(set_id=set_id, chip_id=card_id),
            SimpleNamespace(id=user_id),
            db,
            response,
        )
    return out, response


async def test_route_flag_off_is_unavailable_and_writes_nothing(owner, monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", True)
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", False)
    _write_state(owner.thread_id, **{sc.RING_KEY: [_entry(1)]})
    before = _snapshot(owner.thread_id)
    out, response = await _dismiss_route(owner.user_id, owner.thread_id, "sg_" + "0" * 15 + "1")
    assert out.status == "unavailable" and response.headers["cache-control"] == "no-store"
    assert _snapshot(owner.thread_id) == before


async def test_route_foreign_thread_is_the_usual_404_and_writes_nothing(owner, monkeypatch):
    from app.routes.creation_threads import RuntimeFailure

    _on(monkeypatch)
    _write_state(owner.thread_id, **{sc.RING_KEY: [_entry(1)]})
    before = _snapshot(owner.thread_id)
    with pytest.raises(RuntimeFailure) as caught:
        await _dismiss_route(uuid.uuid4(), owner.thread_id, "sg_" + "0" * 15 + "1")
    assert caught.value.status_code == 404
    assert _snapshot(owner.thread_id) == before
    with pytest.raises(RuntimeFailure) as caught:
        await _dismiss_route(owner.user_id, "not-a-uuid")
    assert caught.value.status_code == 404


async def test_route_owner_dismiss_round_trip(owner, monkeypatch):
    _on(monkeypatch)
    _write_state(owner.thread_id, **{sc.RING_KEY: [_entry(1)]})
    set_id = "sg_" + "0" * 15 + "1"
    out, _ = await _dismiss_route(owner.user_id, owner.thread_id, set_id)
    again, _ = await _dismiss_route(owner.user_id, owner.thread_id, set_id)
    unknown, _ = await _dismiss_route(owner.user_id, owner.thread_id, set_id, "ss_never_offered")
    assert [out.status, again.status, unknown.status] == [
        "dismissed",
        "already_dismissed",
        "unknown",
    ]


async def test_the_public_thread_projection_hides_the_suppressed_list(owner, monkeypatch):
    from app.routes.creation_threads import _response

    _write_state(owner.thread_id, **{sc.RING_KEY: [_entry(1)], serve.SUPPRESSED_KEY: ["abc"]})
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, owner.thread_id)
        public = await _response(db, thread)
    assert sc.RING_KEY not in public.state and serve.SUPPRESSED_KEY not in public.state
    assert public.state["intent"] == "keep me"


# ── The GET's ring write: attach, fold, eviction ─────────────────────────────────────


async def _persist(owner, served, *, protocol=2):
    async with AsyncSessionLocal() as db:
        return await serve.persist_served_set(db, owner.thread_id, served, protocol=protocol)


async def test_the_cards_are_stored_on_the_entry_so_a_tap_and_a_dismiss_find_them(
    owner, monkeypatch
):
    from app.services.suggestions import pinned
    from app.services.suggestions.pinned import ring_suggestions

    async def fake_cards(served):
        return ring_suggestions(SimpleNamespace(suggestions=[MUTE_3, CAPTIONS]))

    monkeypatch.setattr(serve, "_server_cards", fake_cards)
    served = _served(1)
    assert await _persist(owner, served) is True
    state = _snapshot(owner.thread_id)["state"]
    entry = state[sc.RING_KEY][0]
    assert [c["id"] for c in entry["suggestions"]] == ["ss_mute3", "ss_caps"]
    card = pinned.stored_card(state, served.out.set_id, "ss_mute3")
    assert card is not None and card.suggestion.action == MUTE_3.action, "the seam can read it"
    # the next read of the same set writes nothing more
    served.ring_has_cards = True
    before = _snapshot(owner.thread_id)
    assert await _persist(owner, served) is False
    assert _snapshot(owner.thread_id) == before


async def test_a_set_whose_cards_cannot_be_rebuilt_is_stored_without_them(owner, monkeypatch):
    async def none(served):
        return None

    monkeypatch.setattr(serve, "_server_cards", none)
    assert await _persist(owner, _served(1)) is True
    entry = _snapshot(owner.thread_id)["state"][sc.RING_KEY][0]
    assert "suggestions" not in entry and entry["chips"], "the chips still work; a tap is refused"


async def test_the_ring_stays_at_five_and_a_dismissed_card_stays_out_after_it_rolls_off(
    owner, monkeypatch
):
    async def cards(served):
        from app.services.suggestions.pinned import ring_suggestions

        return ring_suggestions(SimpleNamespace(suggestions=[MUTE_3, CAPTIONS]))

    monkeypatch.setattr(serve, "_server_cards", cards)
    assert await _persist(owner, _served(1))
    async with AsyncSessionLocal() as db:
        assert (
            await serve.dismiss_suggestion(db, owner.thread_id, "sg_" + "0" * 15 + "1", "ss_mute3")
            == "dismissed"
        )
    for n in range(2, 9):
        assert await _persist(owner, _served(n))
    state = _snapshot(owner.thread_id)["state"]
    assert [e["set_id"] for e in state[sc.RING_KEY]] == [f"sg_{n:016d}" for n in range(4, 9)]
    assert all(serve.DISMISSED_FIELD not in e for e in state[sc.RING_KEY]), "set 1 is long gone"
    fp = serve.fingerprint("mute_clip", {"media_id": "m3", "clip_ordinal": 3})
    assert fp in serve.suppressed_set(state)
    served = _served(9)
    out = serve._without_suppressed(served, SimpleNamespace(state=state)).out
    assert [s.kind for s in out.suggestions] == ["captions"]


async def test_an_applied_mark_is_folded_before_the_append_can_evict_its_entry(owner, monkeypatch):
    async def none(served):
        return None

    monkeypatch.setattr(serve, "_server_cards", none)
    ring = [_entry(n, (MUTE_3,)) for n in range(1, 6)]
    ring[0]["consumed"] = {"ss_mute3": "turn-1"}  # the oldest entry is the one that was applied
    _write_state(owner.thread_id, **{sc.RING_KEY: ring})
    async with AsyncSessionLocal() as db:
        state = (await db.get(CreationThread, owner.thread_id)).state
    served = _served(6, (CAPTIONS,))
    served = serve._without_suppressed(served, SimpleNamespace(state=state))
    assert served.needs_fold is True
    assert await _persist(owner, served)
    after = _snapshot(owner.thread_id)["state"]
    assert [e["set_id"] for e in after[sc.RING_KEY]][0] == f"sg_{2:016d}", "set 1 was evicted"
    fp = serve.fingerprint("mute_clip", {"media_id": "m3", "clip_ordinal": 3})
    assert after[serve.SUPPRESSED_KEY] == [fp], "...and its applied card is still remembered"


async def test_the_get_writes_never_wait_behind_a_turn(owner, monkeypatch):
    async def cards(served):
        from app.services.suggestions.pinned import ring_suggestions

        return ring_suggestions(SimpleNamespace(suggestions=[MUTE_3]))

    monkeypatch.setattr(serve, "_server_cards", cards)
    before = _snapshot(owner.thread_id)
    holder = AsyncSessionLocal()
    try:
        await holder.execute(
            select(CreationThread.id).where(CreationThread.id == owner.thread_id).with_for_update()
        )
        async with AsyncSessionLocal() as db:
            wrote = await asyncio.wait_for(
                serve.persist_served_set(db, owner.thread_id, _served(1, (MUTE_3,)), protocol=2),
                timeout=8,
            )
    finally:
        await holder.rollback()
        await holder.close()
    assert wrote is False, "a locked thread skips the write; the next read records it"
    assert _snapshot(owner.thread_id) == before
