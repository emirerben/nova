"""The removal provenance survives the real runtime, storage and reload (#1974 follow-up).

Runs on the disposable `*_test` Postgres (the guard lives in the module imported below).

Shared contract, through a real existing server-waiver journey and a real creator turn:
 D1 the Don Julio simpler-version tap (`test_kri968_simpler_version_names_drops_postgres`): the
    requirement the server left out is stored `superseded_by = "server"` and reloads that way; the
    original assertions of that test hold unchanged.
 D2 a creator's removal through the real planner turn is stored `"creator"`.
Order consumption, separately (that journey leaves out a text requirement, and no filming-order ask
is reachable as an omission there; nothing here forces a readiness result):
 D3 a filming-order requirement removed by the server's automatic-omission append reloads
    "server" and keeps filming order; removed by a creator turn it reloads "creator" and does not.
 D4 a fresh start after that stored server waiver is stored "creator" and no longer keeps filming
    order; replaying the same turn (a retry) stores nothing new.
"""

from __future__ import annotations

import uuid

import pytest

from app.database import sync_session
from app.kria.brief import (
    BriefUpdate,
    load_latest_brief_sync,
    persist_brief_version_sync,
    start_fresh_updates,
)
from app.pipeline.unified_montage import brief_view
from app.tasks.kria_runtime import _append_automatic_omissions_sync
from tests.kria.test_kri968_simpler_version_names_drops_postgres import (  # noqa: F401
    EVENTS,
    _dispose_async_engine,
    _flags,
    _held_draft,
    _planned,
    _seed_t1,
    _strategy,
    _tap_simpler,
    _turn,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _submit

MARK = "superseded_by"


@pytest.mark.asyncio
async def test_d1_the_real_simpler_version_tap_stores_a_server_waiver(monkeypatch) -> None:
    user_id, thread_id = _seed_t1()
    held = await _held_draft(monkeypatch, user_id, thread_id)
    await _tap_simpler(monkeypatch, user_id, thread_id, held)
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)  # reloaded from storage
    # The original assertions of the existing tap test, unchanged.
    assert brief.version == 4
    assert [r.id for r in brief.requirements if not r.live] == ["r4"]
    r4 = next(r for r in brief.requirements if r.id == "r4")
    assert r4.facts.get(MARK) == "server"
    assert all(MARK not in r.facts for r in brief.live())


@pytest.mark.asyncio
async def test_d2_a_creators_removal_through_the_real_planner_is_stored_as_theirs(
    monkeypatch,
) -> None:
    user_id, thread_id = _seed_t1()
    removal = BriefUpdate(operation="remove", target_requirement_id="r6", expected_version=3)
    choice = EVENTS[9]["payload"]["choice_selection"]
    await _turn(
        monkeypatch,
        user_id,
        thread_id,
        EVENTS[9]["content"],
        _planned(_strategy(), removal),
        choice=(choice["question_id"], choice["option_key"]),
    )
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
    r6 = next(r for r in brief.requirements if r.id == "r6")
    assert (r6.status, r6.facts.get(MARK)) == ("superseded", "creator")


ORDER = BriefUpdate(
    operation="add",
    kind="order",
    scope="global",
    description="in the order I filmed it",
    facts={"key": "capture_time"},
)


async def _brief_with_a_filming_ask():  # noqa: ANN202
    user_id, thread_id, _session = _seed_runtime_project()
    first = await _submit(user_id, thread_id, "Montage in the order I filmed it", 2)
    with sync_session() as db:
        brief = persist_brief_version_sync(
            db, thread_id=thread_id, turn_id=uuid.UUID(first.turn_id), updates=(ORDER,)
        )
        db.commit()
    return user_id, thread_id, brief


@pytest.mark.asyncio
async def test_d3_the_automatic_omission_append_stores_server_and_keeps_filming_order() -> None:
    _user, thread_id, brief = await _brief_with_a_filming_ask()
    with sync_session() as db:
        _append_automatic_omissions_sync(
            db,
            thread_id=thread_id,
            brief=brief,
            updates=(
                BriefUpdate(
                    operation="remove", target_requirement_id="r1", expected_version=brief.version
                ),
            ),
        )
        db.commit()
        reloaded = load_latest_brief_sync(db, thread_id)
    r1 = reloaded.requirements[0]
    assert (r1.status, r1.facts[MARK]) == ("superseded", "server")
    assert brief_view(reloaded).kept_filming_order is True


@pytest.mark.asyncio
async def test_d3_a_creator_turn_removal_is_stored_creator_and_is_not_kept() -> None:
    user_id, thread_id, brief = await _brief_with_a_filming_ask()
    second = await _submit(user_id, thread_id, "Forget the filming order", 3)
    with sync_session() as db:
        persist_brief_version_sync(
            db,
            thread_id=thread_id,
            turn_id=uuid.UUID(second.turn_id),
            updates=(
                BriefUpdate(
                    operation="remove", target_requirement_id="r1", expected_version=brief.version
                ),
            ),
        )
        db.commit()
        reloaded = load_latest_brief_sync(db, thread_id)
    assert reloaded.requirements[0].facts[MARK] == "creator"
    assert brief_view(reloaded).kept_filming_order is False


@pytest.mark.asyncio
async def test_d4_a_fresh_start_after_a_stored_waiver_is_creators_and_idempotent() -> None:
    user_id, thread_id, brief = await _brief_with_a_filming_ask()
    with sync_session() as db:
        _append_automatic_omissions_sync(
            db,
            thread_id=thread_id,
            brief=brief,
            updates=(
                BriefUpdate(
                    operation="remove", target_requirement_id="r1", expected_version=brief.version
                ),
            ),
        )
        db.commit()
        waived = load_latest_brief_sync(db, thread_id)
    assert waived.requirements[0].facts[MARK] == "server"
    assert brief_view(waived).kept_filming_order is True

    turn_id = uuid.UUID((await _submit(user_id, thread_id, "Start over", 3)).turn_id)
    fresh = start_fresh_updates(waived, ())
    with sync_session() as db:
        persist_brief_version_sync(db, thread_id=thread_id, turn_id=turn_id, updates=fresh)
        db.commit()
        once = load_latest_brief_sync(db, thread_id)
        persist_brief_version_sync(db, thread_id=thread_id, turn_id=turn_id, updates=fresh)
        db.commit()
        again = load_latest_brief_sync(db, thread_id)
    assert once.requirements[0].facts[MARK] == "creator"
    assert brief_view(once).kept_filming_order is False
    assert (again.version, again.requirements) == (once.version, once.requirements)
