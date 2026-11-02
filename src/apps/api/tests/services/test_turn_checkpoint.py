"""What a continued turn's stored planner answers may reveal, and when they stop applying.

Failure modes, written before the code:

- a stored answer that no longer validates (schema drift after a deploy) is logged with
  Pydantic's message, which quotes the offending value: the Main Creator is sensitive_io, so
  creator words reach the logs. A drifted answer is a silent miss;
- the key omits something that changes the answer for the same input (thinking level, output
  mode, release): a continued run gets a plausible answer from the old behaviour;
- the key cannot be computed (an input that does not serialise): the turn must still run.

Stub Redis; no network.
"""

from __future__ import annotations

import json
import traceback
import uuid
from typing import Any

import pytest
from pydantic import BaseModel

from app.services import turn_checkpoint

PRIVATE = "Creator private words: meet me at 5 Calle Mayor"


class _Answer(BaseModel):
    reply: int


class _Redis:
    def __init__(self, stored: dict[str, bytes] | None = None) -> None:
        self.stored = stored or {}

    def hget(self, _key: str, field: str) -> bytes | None:
        return self.stored.get(field)


class _RecordingLog:
    """Renders what a real renderer prints: the message, and the traceback when asked for."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def warning(self, event: str, **kw: Any) -> None:
        text = f"{event} {kw}"
        if kw.get("exc_info"):
            text += traceback.format_exc()
        self.lines.append(text)

    info = debug = warning


def test_a_stored_answer_that_no_longer_validates_logs_no_creator_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = json.dumps({"v": 1, "output": json.dumps({"reply": PRIVATE})}).encode()
    monkeypatch.setattr(turn_checkpoint, "_redis", lambda: _Redis({"main_creator:x": envelope}))
    recorded = _RecordingLog()
    monkeypatch.setattr(turn_checkpoint, "log", recorded)

    stored = turn_checkpoint._load(uuid.uuid4(), "main_creator:x", _Answer)

    assert stored is None
    assert "Calle Mayor" not in "\n".join(recorded.lines)


class _Pipe:
    def __init__(self, store: _MemoryRedis) -> None:
        self.store, self.ops = store, []

    def hset(self, key: str, field: str, value: str) -> None:
        self.ops.append(("hset", key, field, value))

    def expire(self, key: str, _seconds: int) -> None:
        self.ops.append(("expire", key))

    def execute(self) -> None:
        for op, key, *rest in self.ops:
            if op == "hset":
                self.store.hashes.setdefault(key, {})[rest[0]] = rest[1].encode()


class _MemoryRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, bytes]] = {}

    def hget(self, key: str, field: str) -> bytes | None:
        return self.hashes.get(key, {}).get(field)

    def pipeline(self) -> _Pipe:
        return _Pipe(self)

    def delete(self, key: str) -> None:
        self.hashes.pop(key, None)


class _In(BaseModel):
    text: str


class _Out(BaseModel):
    reply: str


def _agent(**spec: Any) -> type:
    base = {
        "name": "fake.agent",
        "prompt_version": "v1",
        "model": "m",
        "fallback_models": (),
        "thinking_level": "high",
        "thinking_budget": None,
    }
    return type("Agent", (), {"spec": type("Spec", (), {**base, **spec})()})


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch) -> _MemoryRedis:
    store = _MemoryRedis()
    monkeypatch.setattr(turn_checkpoint, "_redis", lambda: store)
    monkeypatch.setenv("FLY_IMAGE_REF", "registry.fly.io/nova-video:deployment-A")
    return store


async def _run(*, epoch: int, agent: type, agent_input: Any = None, out_cls: type = _Out, **kw):
    from app.kria.paid_call_scope import turn_run_epoch  # noqa: PLC0415

    calls = []

    async def call() -> BaseModel:
        calls.append(1)
        return out_cls(reply="answer")

    token = turn_checkpoint.turn_run_id.set(_TURN)
    epoch_token = turn_run_epoch.set(epoch)
    try:
        await turn_checkpoint.checkpointed(
            "fake",
            agent_cls=agent,
            agent_input=agent_input or _In(text="hello"),
            output_cls=out_cls,
            call=call,
            **kw,
        )
    finally:
        turn_run_epoch.reset(epoch_token)
        turn_checkpoint.turn_run_id.reset(token)
    return len(calls)


_TURN = uuid.uuid4()


async def test_the_same_call_is_reused_by_a_continued_run(memory: _MemoryRedis) -> None:
    assert await _run(epoch=1, agent=_agent()) == 1
    assert await _run(epoch=2, agent=_agent()) == 0


@pytest.mark.parametrize(
    "changed",
    [
        {"agent": _agent(thinking_level="low")},
        {"agent": _agent(thinking_budget=256)},
        {"agent": _agent(fallback_models=("other",))},
        {"agent": _agent(), "variant": "native"},
    ],
    ids=["thinking_level", "thinking_budget", "fallback_models", "output_mode"],
)
async def test_a_changed_call_setting_is_a_different_key(
    memory: _MemoryRedis, changed: dict[str, Any]
) -> None:
    await _run(epoch=1, agent=_agent())

    assert await _run(epoch=2, **changed) == 1


async def test_a_new_release_is_a_different_key(
    memory: _MemoryRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run(epoch=1, agent=_agent())
    monkeypatch.setenv("FLY_IMAGE_REF", "registry.fly.io/nova-video:deployment-B")

    assert await _run(epoch=2, agent=_agent()) == 1


async def test_a_changed_answer_schema_is_a_different_key(memory: _MemoryRedis) -> None:
    class _Out(BaseModel):  # same class name, one more field
        reply: str
        extra: int = 0

    await _run(epoch=1, agent=_agent())

    assert await _run(epoch=2, agent=_agent(), out_cls=_Out) == 1


async def test_a_key_that_cannot_be_computed_still_runs_the_call(
    memory: _MemoryRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("cannot serialise")

    monkeypatch.setattr(turn_checkpoint, "_digest", broken)

    assert await _run(epoch=2, agent=_agent()) == 1
