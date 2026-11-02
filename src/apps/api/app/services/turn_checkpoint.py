"""What a continued Kria turn keeps from its earlier runs, and what it records about them.

A turn that asks to run again (KRI-709 continuation) used to re-send every planner call. PR 1
made those calls legal (a later run is a new paid call); this module makes them unnecessary:
a successful planner answer is stored under (turn, call, digest of the call's whole input),
and a run that is itself a continuation (lease epoch > 1) takes the stored answer instead of
calling the model. A changed input is a different digest, so it calls.

The store is a fail-open Redis cache (same pattern as `app.kria.research_preview`): every
failure is a miss or a skipped write, never a failed turn, because a miss is always safe now
(it pays again under its own epoch-scoped key). It is therefore NOT the source of truth for
anything; the turn row's `planning_runs` records what happened, and holds no creator text.

`TurnRunRecorder` collects (name, ms, source) for the calls and waits of the running run so
the continuation decision can be written to the turn and read from `kria/trace`.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import functools
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import structlog
from pydantic import BaseModel

from app.config import settings
from app.kria.paid_call_scope import turn_run_epoch

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)

# Longer than the whole continuation chain (runs of <= 200 s with 5 s gaps, 4 runs).
CHECKPOINT_TTL_S = 1800
_FORMAT = 1

# The turn the running planner belongs to (set by the turn task next to the run epoch).
turn_run_id: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "kria_turn_run_id", default=None
)


class TurnRunRecorder:
    """Calls and waits of one run, in order: ``{"name", "ms", "source"}``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def note(self, name: str, *, ms: float, source: str) -> None:
        self.calls.append({"name": name, "ms": int(ms), "source": source})


turn_run_recorder: contextvars.ContextVar[TurnRunRecorder | None] = contextvars.ContextVar(
    "kria_turn_run_recorder", default=None
)


def current_run_recorder() -> TurnRunRecorder | None:
    return turn_run_recorder.get()


def note_wait(name: str, *, seconds: float) -> None:
    recorder = turn_run_recorder.get()
    if recorder is not None:
        recorder.note(name, ms=seconds * 1000, source="wait")


def _redis():  # noqa: ANN202
    import redis  # noqa: PLC0415

    return redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)


def _hash_key(run_id: uuid.UUID) -> str:
    """One Redis hash per turn: its fields are the stored answers, so ending the turn is one DEL."""
    return f"kria:turn-checkpoint:{run_id}"


def _field(call_name: str, digest: str) -> str:
    return f"{call_name}:{digest}"


@functools.lru_cache(maxsize=64)
def _schema_digest(*classes: type[BaseModel]) -> str:
    schemas = [cls.model_json_schema() for cls in classes]
    return hashlib.sha256(json.dumps(schemas, sort_keys=True, default=str).encode()).hexdigest()


def _release() -> str:
    """The deployed image (Fly sets it per release): a deploy never reads an older code's answer."""
    return os.environ.get("FLY_IMAGE_REF", "")


def _digest(
    call_name: str,
    spec: Any,
    variant: str,
    agent_input: BaseModel,
    output_cls: type[BaseModel],
) -> str:
    """Everything that can make the same input produce a different answer.

    The class-level spec (name, prompt version, model, fallbacks, thinking), the runtime
    override the caller names in ``variant`` (Main Creator's output mode changes the prompt and
    the parse), both schemas, and the deployed image: for up to the TTL after a deploy or a
    flag change a continued run would otherwise take a plausible answer from the old behaviour.
    """

    payload = json.dumps(
        {
            "call": call_name,
            "agent": f"{spec.name}@{spec.prompt_version}:{spec.model}",
            "fallbacks": list(getattr(spec, "fallback_models", ()) or ()),
            "thinking": [
                getattr(spec, "thinking_level", None),
                getattr(spec, "thinking_budget", None),
            ],
            "variant": variant,
            "schemas": _schema_digest(type(agent_input), output_cls),
            "release": _release(),
            "input": agent_input.model_dump(mode="json"),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load(run_id: uuid.UUID, field: str, output_cls: type[T]) -> T | None:
    try:
        raw = _redis().hget(_hash_key(run_id), field)
        if raw is None:
            return None
        envelope = json.loads(raw)
        if envelope.get("v") != _FORMAT:
            return None
        return output_cls.model_validate_json(envelope["output"])
    except (ValueError, KeyError, TypeError, AttributeError):
        # Drifted or corrupt: a miss. Say nothing about the content: Pydantic's message quotes
        # the offending value, and Main Creator answers carry creator words (sensitive_io).
        return None
    except Exception:  # noqa: BLE001 - an unreachable store is a miss
        log.warning("kria_turn_checkpoint_unreadable", exc_info=True)
        return None


def _store(run_id: uuid.UUID, field: str, output: BaseModel) -> None:
    try:
        envelope = json.dumps({"v": _FORMAT, "output": output.model_dump_json()})
        key = _hash_key(run_id)
        pipe = _redis().pipeline()
        pipe.hset(key, field, envelope)
        pipe.expire(key, CHECKPOINT_TTL_S)
        pipe.execute()
    except Exception:  # noqa: BLE001 - a skipped write costs one repeated call, nothing else
        log.warning("kria_turn_checkpoint_store_failed", exc_info=True)


def discard(run_id: uuid.UUID) -> None:
    """Drop everything a turn stored once it is over; the TTL is only the backstop."""
    try:
        _redis().delete(_hash_key(run_id))
    except Exception:  # noqa: BLE001 - the TTL removes it anyway
        log.warning("kria_turn_checkpoint_discard_failed", exc_info=True)


async def checkpointed(
    call_name: str,
    *,
    agent_cls: object,
    agent_input: object,
    output_cls: type[T],
    call: Callable[[], Awaitable[T]],
    variant: str = "",
) -> T:
    """Run ``call`` unless an earlier run of this turn already got its answer for this input.

    Outside a turn task (no turn id), for an input that is not a model, or for an agent
    without a spec, this is just ``call()``, and so is a key that cannot be computed. The key is
    described on `_digest`; ``variant`` names a runtime override the class spec does not show.
    """

    started = time.monotonic()
    run_id = turn_run_id.get()
    epoch = turn_run_epoch.get()
    spec = getattr(agent_cls, "spec", None)
    if run_id is None or not isinstance(agent_input, BaseModel) or spec is None:
        return await call()
    try:
        field = _field(call_name, _digest(call_name, spec, variant, agent_input, output_cls))
    except Exception as exc:  # noqa: BLE001 - no key means no checkpoint, never a failed turn
        log.warning("kria_turn_checkpoint_key_failed", error_class=type(exc).__name__)
        return await call()
    if epoch > 1:
        stored = await asyncio.to_thread(_load, run_id, field, output_cls)
        if stored is not None:
            _note(call_name, started, "checkpoint")
            return stored
    output = await call()
    _note(call_name, started, "model")
    # The last permitted run is never continued, so nothing could read what it would store.
    if epoch < int(settings.kria_turn_auto_continue_max_runs):
        await asyncio.to_thread(_store, run_id, field, output)
    return output


def _note(name: str, started: float, source: str) -> None:
    recorder = turn_run_recorder.get()
    if recorder is not None:
        recorder.note(name, ms=(time.monotonic() - started) * 1000, source=source)
