"""A continued turn leaves its reason and timings on the turn, readable from `kria/trace`.

Prod thread b61b7673 (2026-11-02): two turns ended "ran out of time" after 133-144 s and the
cause (a 45 s clip-analysis wait that could never end, then a continuation) was reconstructable
only from the item's agent_run rows plus a guess. The runtime now records every continuation:
which run, why it asked to run again and what its calls and waits cost.

Real `run_kria_turn` / `_continue_turn` / trace builder on Postgres; the planner is scripted.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace

import pytest

from app.database import AsyncSessionLocal, sync_session
from app.models import CreatorAgentTurn
from app.services.kria_trace import resolve_kria_trace
from app.services.turn_checkpoint import current_run_recorder
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
from tests.kria.test_turn_auto_continue_postgres import (  # noqa: F401
    FINAL_REPLY,
    _publishes,
    _respond,
    _runtime_enabled,
    _script_planner,
    _submit,
)


@pytest.mark.asyncio
async def test_a_continuation_records_why_and_what_it_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    pending = replace(
        _respond("Still checking your clips.", continuable=True),
        continue_reason="clip_analysis_pending",
    )

    async def planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        recorder = current_run_recorder()
        assert recorder is not None  # the turn task opened one for this run
        recorder.note("clip_understanding_wait", ms=45_400, source="wait")
        recorder.note("main_creator", ms=43_227, source="model")
        return pending if not published else _respond(FINAL_REPLY, continuable=False)

    published = _publishes(monkeypatch)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planned)
    accepted = await _submit(user_id, thread_id, "Start with the cooking clip.", 2)

    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert result["status"] == "continued"
    with sync_session() as db:
        runs = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id)).planning_runs
    assert len(runs) == 1
    assert runs[0]["epoch"] == 1
    assert runs[0]["outcome"] == "continued"
    assert runs[0]["reason"] == "clip_analysis_pending"
    assert runs[0]["calls"] == [
        {"name": "clip_understanding_wait", "ms": 45_400, "source": "wait"},
        {"name": "main_creator", "ms": 43_227, "source": "model"},
    ]

    async with AsyncSessionLocal() as db:
        trace = await resolve_kria_trace(db, turn_id=uuid.UUID(accepted.turn_id))
    assert trace["turns"][0]["planning_runs"] == runs
