"""Offline request lifecycle: the fake paid function never contacts a provider."""

import asyncio
import copy
import uuid

import pytest

from app.services.slide_post_ai_requests import AIExecution, AIReceiptConflict


class MemoryStore:
    def __init__(self):
        self.row = {"status": "pending", "claim_id": None, "resolved_input": None, "result": None}

    async def claim(self, request_id, snapshot):
        if self.row["status"] != "pending":
            return None
        self.row.update(
            status="running", claim_id=str(uuid.uuid4()), resolved_input=copy.deepcopy(snapshot)
        )
        return self.row["claim_id"]

    async def complete(self, request_id, claim_id, result):
        if self.row["claim_id"] != claim_id:
            return False
        self.row.update(status="completed", result=copy.deepcopy(result))
        return True

    async def unknown(self, request_id, claim_id):
        if self.row["claim_id"] == claim_id and self.row["status"] == "running":
            self.row["status"] = "unknown"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def deny(*args, **kwargs):
        raise AssertionError("network forbidden in AI request lifecycle test")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


@pytest.mark.asyncio
async def test_lost_completed_response_retry_observes_without_second_paid_call():
    store = MemoryStore()
    request_id = uuid.uuid4()
    calls = 0

    async def fake_paid():
        nonlocal calls
        calls += 1
        return {"draft": {"caption": "one result"}}

    first = AIExecution(store, request_id)
    result = await first.run({"prompt": "caption"}, fake_paid)
    await first.complete(result)  # HTTP response is deliberately discarded.
    retry = AIExecution(store, request_id)
    with pytest.raises(AIReceiptConflict):
        await retry.run({"prompt": "caption"}, fake_paid)
    assert store.row["result"] == result
    assert calls == 1


@pytest.mark.asyncio
async def test_unknown_claim_is_never_reexecuted_but_late_result_can_complete():
    store = MemoryStore()
    request_id = uuid.uuid4()
    calls = 0

    async def fake_paid():
        nonlocal calls
        calls += 1
        raise TimeoutError("response unknown")

    first = AIExecution(store, request_id)
    with pytest.raises(TimeoutError):
        await first.run({"prompt": "caption"}, fake_paid)
    assert store.row["status"] == "unknown"
    with pytest.raises(AIReceiptConflict):
        await AIExecution(store, request_id).run({"prompt": "caption"}, fake_paid)
    assert calls == 1
    await first.complete({"verified": "late original result"})
    assert store.row["status"] == "completed"


@pytest.mark.asyncio
async def test_pending_can_be_claimed_once_and_snapshot_is_copied():
    store = MemoryStore()
    request_id = uuid.uuid4()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_paid():
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"ok": True}

    snapshot = {"draft": {"caption": "original"}}
    first = AIExecution(store, request_id)
    task = asyncio.create_task(first.run(snapshot, fake_paid))
    await entered.wait()
    snapshot["draft"]["caption"] = "changed"
    with pytest.raises(AIReceiptConflict):
        await AIExecution(store, request_id).run(snapshot, fake_paid)
    release.set()
    await task
    assert calls == 1
    assert store.row["resolved_input"]["draft"]["caption"] == "original"
