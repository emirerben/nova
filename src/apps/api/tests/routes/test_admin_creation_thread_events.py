"""Admin creation-thread transcript views: ``/events`` and ``/turns`` (read-only).

The DB is a small fake that routes each statement by the table it selects from and
records the compiled SQL, so ordering / limit / cursor behaviour is asserted on the
real generated statement rather than only on canned rows.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app

ADMIN_TOKEN = "test-admin-token"
HEADERS = {"X-Admin-Token": ADMIN_TOKEN}
T0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _thread(runtime_version: int = 2) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), runtime_version=runtime_version)


def _event(thread_id, sequence: int, **overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        thread_id=thread_id,
        sequence=sequence,
        client_event_id=f"c{sequence}",
        role="user",
        event_type="user_message",
        content=f"message {sequence}",
        payload={"chips": ["a"]},
        revision=sequence + 1,
        created_at=T0 + timedelta(seconds=sequence),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _turn(thread_id, n: int, source_event_id, **overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        thread_id=thread_id,
        session_id=None,
        source_event_id=source_event_id,
        observed_event_id=None,
        client_event_id=f"c{n}",
        status="completed",
        plan_json={"turn": n, "steps": []},
        error=None,
        created_at=T0 + timedelta(seconds=n),
        completed_at=T0 + timedelta(seconds=n + 1),
        cancel_requested_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _execution(turn_id, **overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        turn_id=turn_id,
        tool_name="apply_edit",
        tool_version=1,
        risk="low",
        status="succeeded",
        dependency_group=0,
        group_order=0,
        target_job_id=None,
        target_variant_id=None,
        target_draft_id=None,
        external_task_id=None,
        result={"ok": True},
        error=None,
        created_at=T0,
        completed_at=T0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeDb:
    """Routes ``execute`` by the first table in the statement's FROM clause."""

    def __init__(self, *, thread, events=(), turns=(), executions=(), turn_rows=(), briefs=()):
        self.thread = thread
        self.events = list(events)
        self.turns = list(turns)
        self.executions = list(executions)
        self.turn_rows = list(turn_rows)
        self.briefs = list(briefs)
        self.statements: list[tuple[str, dict]] = []

    async def execute(self, stmt, *args, **kwargs):
        compiled = stmt.compile()
        sql = str(compiled)
        self.statements.append((sql, dict(compiled.params)))
        result = MagicMock()
        from_table = re.search(r"\bFROM\s+(\w+)", sql).group(1)
        select_list = sql.split("FROM", 1)[0]
        if from_table == "creation_threads":
            result.scalar_one_or_none.return_value = self.thread
        elif from_table == "creation_thread_events":
            result.scalars.return_value.all.return_value = self.events
        elif (
            from_table == "creator_agent_turns"
            and "creator_agent_turns.thread_id" not in select_list
        ):
            result.all.return_value = self.turn_rows
        elif from_table == "creator_agent_turns":
            result.scalars.return_value.all.return_value = self.turns
        elif from_table == "creator_agent_executions":
            result.scalars.return_value.all.return_value = self.executions
        elif from_table == "creative_brief_versions":
            result.all.return_value = self.briefs
        else:  # pragma: no cover - guards against an unexpected extra query
            raise AssertionError(f"unexpected statement: {sql}")
        return result

    def stmt(self, table: str) -> tuple[str, dict]:
        return next(s for s in self.statements if re.search(rf"\bFROM\s+{table}\b", s[0]))


def _get(client: TestClient, db: FakeDb, path: str, *, headers=HEADERS):
    async def _gen():
        yield db

    app.dependency_overrides[get_db] = _gen
    try:
        with patch("app.routes.admin.settings") as settings:
            settings.admin_api_key = ADMIN_TOKEN
            return client.get(path, headers=headers)
    finally:
        app.dependency_overrides.pop(get_db, None)


# ── /events ──────────────────────────────────────────────────────────────────


def test_events_returns_sequence_order_with_payload_shape(client: TestClient) -> None:
    thread = _thread()
    events = [
        _event(thread.id, 0, payload={"chips": ["fast"], "brief": {"n": 1}}),
        _event(
            thread.id,
            1,
            role="assistant",
            event_type="assistant_message",
            content="Done",
            payload={
                "receipt": {"tool": "apply_edit", "ok": True},
                "preview": "https://bucket/x.mp4?X-Goog-Signature=abc",
            },
        ),
    ]
    turn_id = uuid.uuid4()
    db = FakeDb(
        thread=thread,
        events=events,
        turn_rows=[(turn_id, events[0].id)],
        briefs=[(turn_id, 3)],
    )

    res = _get(client, db, f"/admin/creation-threads/{thread.id}/events")

    assert res.status_code == 200
    body = res.json()
    assert body["thread_id"] == str(thread.id)
    assert body["runtime_version"] == 2
    assert body["next_cursor"] is None
    assert [e["sequence"] for e in body["events"]] == [0, 1]
    first, second = body["events"]
    assert set(first) == {
        "id",
        "sequence",
        "kind",
        "actor",
        "revision",
        "created_at",
        "client_event_id",
        "content",
        "payload",
        "turn_id",
        "brief_version",
    }
    assert first["kind"] == "user_message"
    assert first["actor"] == "user"
    assert first["content"] == "message 0"
    assert first["payload"] == {"chips": ["fast"], "brief": {"n": 1}}
    assert first["created_at"] == events[0].created_at.isoformat()
    assert first["turn_id"] == str(turn_id)
    assert first["brief_version"] == 3
    assert second["turn_id"] is None and second["brief_version"] is None
    assert second["payload"]["receipt"] == {"tool": "apply_edit", "ok": True}
    assert second["payload"]["preview"] == "[redacted-signed-url]"

    sql, _params = db.stmt("creation_thread_events")
    assert "ORDER BY creation_thread_events.sequence" in sql


def test_events_paginates_with_sequence_cursor(client: TestClient) -> None:
    thread = _thread()
    # limit=2 -> the route asks for 3 rows; the third proves there is a next page.
    rows = [_event(thread.id, s) for s in (5, 6, 7)]
    db = FakeDb(thread=thread, events=rows)

    res = _get(client, db, f"/admin/creation-threads/{thread.id}/events?limit=2&cursor=4")

    assert res.status_code == 200
    body = res.json()
    assert [e["sequence"] for e in body["events"]] == [5, 6]
    assert body["next_cursor"] == "6"
    sql, params = db.stmt("creation_thread_events")
    assert "creation_thread_events.sequence >" in sql
    assert 4 in params.values()
    assert 3 in params.values()  # limit + 1


def test_events_last_page_has_no_cursor(client: TestClient) -> None:
    thread = _thread()
    db = FakeDb(thread=thread, events=[_event(thread.id, 9)])
    res = _get(client, db, f"/admin/creation-threads/{thread.id}/events?limit=2&cursor=8")
    assert res.status_code == 200
    assert res.json()["next_cursor"] is None


def test_events_limit_bounds(client: TestClient) -> None:
    thread = _thread()
    db = FakeDb(thread=thread)
    ok = _get(client, db, f"/admin/creation-threads/{thread.id}/events?limit=500")
    assert ok.status_code == 200
    for bad in ("0", "501"):
        res = _get(client, db, f"/admin/creation-threads/{thread.id}/events?limit={bad}")
        assert res.status_code == 422


@pytest.mark.parametrize("cursor", ["abc", "-1"])
def test_events_rejects_bad_cursor(client: TestClient, cursor: str) -> None:
    thread = _thread()
    res = _get(
        client,
        FakeDb(thread=thread),
        f"/admin/creation-threads/{thread.id}/events?cursor={cursor}",
    )
    assert res.status_code == 422
    assert res.json()["detail"] == "invalid_cursor"


def test_events_unknown_thread_404_and_malformed_id_422(client: TestClient) -> None:
    missing = _get(client, FakeDb(thread=None), f"/admin/creation-threads/{uuid.uuid4()}/events")
    assert missing.status_code == 404
    bad = _get(client, FakeDb(thread=None), "/admin/creation-threads/not-a-uuid/events")
    assert bad.status_code == 422


@pytest.mark.parametrize("suffix", ["events", "turns"])
def test_transcript_routes_require_admin_auth(client: TestClient, suffix: str) -> None:
    db = FakeDb(thread=_thread())
    path = f"/admin/creation-threads/{uuid.uuid4()}/{suffix}"
    assert _get(client, db, path, headers={}).status_code in {401, 422}
    assert _get(client, db, path, headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert db.statements == []


# ── /turns ───────────────────────────────────────────────────────────────────


def test_turns_expose_plan_receipts_and_linked_jobs(client: TestClient) -> None:
    thread = _thread()
    src1, src2 = uuid.uuid4(), uuid.uuid4()
    t1, t2 = _turn(thread.id, 1, src1), _turn(thread.id, 2, src2, status="failed")
    job_id = uuid.uuid4()
    executions = [
        _execution(t1.id, target_job_id=job_id, result={"ok": True, "url": "x?X-Amz-Signature=1"}),
        _execution(t1.id, tool_name="set_text", group_order=1, target_job_id=job_id),
        _execution(t2.id, status="failed", error={"code": "capability_unavailable"}),
    ]
    db = FakeDb(thread=thread, turns=[t1, t2], executions=executions, briefs=[(t1.id, 2)])

    res = _get(client, db, f"/admin/creation-threads/{thread.id}/turns")

    assert res.status_code == 200
    body = res.json()
    assert body["thread_id"] == str(thread.id)
    assert body["runtime_version"] == 2
    assert body["next_cursor"] is None
    first, second = body["turns"]
    assert [first["id"], second["id"]] == [str(t1.id), str(t2.id)]
    assert first["plan"] == {"turn": 1, "steps": []}
    assert first["status"] == "completed"
    assert first["source_event_id"] == str(src1)
    assert first["brief_version"] == 2
    assert first["job_ids"] == [str(job_id)]  # de-duplicated
    assert [e["tool_name"] for e in first["executions"]] == ["apply_edit", "set_text"]
    assert first["executions"][0]["result"] == {"ok": True, "url": "[redacted-signed-url]"}
    assert second["status"] == "failed"
    assert second["job_ids"] == []
    assert second["executions"][0]["error"] == {"code": "capability_unavailable"}
    assert second["brief_version"] is None

    sql, _ = db.stmt("creator_agent_turns")
    assert "ORDER BY creator_agent_turns.created_at, creator_agent_turns.id" in sql


def test_turns_paginate_with_created_at_id_cursor(client: TestClient) -> None:
    thread = _thread()
    turns = [_turn(thread.id, n, uuid.uuid4()) for n in (1, 2, 3)]
    db = FakeDb(thread=thread, turns=turns)

    first = _get(client, db, f"/admin/creation-threads/{thread.id}/turns?limit=2")
    assert first.status_code == 200
    body = first.json()
    assert [t["id"] for t in body["turns"]] == [str(turns[0].id), str(turns[1].id)]
    assert body["next_cursor"]

    db2 = FakeDb(thread=thread, turns=[turns[2]])
    second = _get(
        client,
        db2,
        f"/admin/creation-threads/{thread.id}/turns?limit=2&cursor={body['next_cursor']}",
    )
    assert second.status_code == 200
    assert [t["id"] for t in second.json()["turns"]] == [str(turns[2].id)]
    assert second.json()["next_cursor"] is None
    sql, _ = db2.stmt("creator_agent_turns")
    assert "(creator_agent_turns.created_at, creator_agent_turns.id) >" in sql


def test_turns_rejects_bad_cursor_and_bounds(client: TestClient) -> None:
    thread = _thread()
    db = FakeDb(thread=thread)
    bad = _get(client, db, f"/admin/creation-threads/{thread.id}/turns?cursor=%%%")
    assert bad.status_code == 422
    for limit in ("0", "201"):
        res = _get(client, db, f"/admin/creation-threads/{thread.id}/turns?limit={limit}")
        assert res.status_code == 422


def test_turns_runtime_v1_thread_returns_empty_list(client: TestClient) -> None:
    thread = _thread(runtime_version=1)
    res = _get(client, FakeDb(thread=thread), f"/admin/creation-threads/{thread.id}/turns")
    assert res.status_code == 200
    assert res.json()["turns"] == []
    assert res.json()["runtime_version"] == 1


def test_turns_unknown_thread_404(client: TestClient) -> None:
    res = _get(client, FakeDb(thread=None), f"/admin/creation-threads/{uuid.uuid4()}/turns")
    assert res.status_code == 404


# ── find_thread_link ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_thread_link_queries_by_item_or_job() -> None:
    from app.services.kria_trace import find_thread_link

    thread_id = uuid.uuid4()
    db = MagicMock()
    result = MagicMock()
    result.first.return_value = (thread_id, 2)
    db.execute = AsyncMock(return_value=result)

    out = await find_thread_link(db, plan_item_id=uuid.uuid4(), job_id=uuid.uuid4())

    assert out == (str(thread_id), 2)
    sql = str(db.execute.await_args.args[0])
    assert "creation_threads.active_plan_item_id" in sql
    assert "creation_threads.active_job_id" in sql
    assert " OR " in sql

    result.first.return_value = None
    assert await find_thread_link(db, plan_item_id=uuid.uuid4()) is None
    # No identity at all: no query is issued.
    db.execute.reset_mock()
    assert await find_thread_link(db) is None
    db.execute.assert_not_awaited()
