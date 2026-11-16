"""A replay turn whose lease lapsed is taken back, or reported as a stall (KRI-1375).

Verifier run 1 on job 1cb33402: the first turn lost its 15 s lease while a cold worker loaded
CLIP, `run_kria_turn` returned `ignored`, and the turn sat `planning` until the replay's timeout,
which then read as a route divergence. Production's beat sweeps such a turn every minute.

Ways this can go wrong, checked on Postgres:
* the replay never sweeps, so a lapsed turn waits out the whole timeout;
* the timeout is reported without saying the lease lapsed, so it reads as the job's result.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.models import CreatorAgentTurn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

SCRIPTS = Path(__file__).parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("replay_creator", SCRIPTS / "replay_creator.py")
assert spec and spec.loader
replay_creator = importlib.util.module_from_spec(spec)
sys.modules.setdefault("replay_creator", replay_creator)
spec.loader.exec_module(replay_creator)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr("app.routes.kria_runtime._publish_turn", lambda _turn_id: None)


async def _lapsed_turn() -> uuid.UUID:
    user_id, thread_id, _ = _seed_runtime_project()
    turn_id = str(uuid.uuid4())
    recorded = replay_creator.RecordedTurn(
        turn_id=turn_id,
        message="Make a montage of the walk",
        client_protocol=15,
        payload={"turn_id": turn_id, "client_protocol": 15},
        approved=False,
    )
    try:
        replay_turn, _ = await replay_creator.submit_recorded_turn(
            thread_id=thread_id,
            user_id=user_id,
            turn=recorded,
            ids=replay_creator.ReplayIds(questions={}, media={}),
        )
    finally:
        await async_engine.dispose()
    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, uuid.UUID(replay_turn))
        turn.status = "planning"
        turn.lease_owner = "cold-worker"
        turn.lease_expires_at = datetime.now(UTC) - timedelta(minutes=2)
        db.commit()
    return uuid.UUID(replay_turn)


@pytest.mark.asyncio
async def test_the_replays_sweep_republishes_a_turn_whose_lease_lapsed(monkeypatch) -> None:
    turn_id = await _lapsed_turn()
    published: list[str] = []
    monkeypatch.setattr(
        "app.tasks.kria_runtime.run_kria_turn.apply_async",
        lambda args, **_: published.append(args[0]),
    )

    beat = replay_creator.ReconcileBeat(interval_s=0)
    # One sweep takes the 50 oldest due turns; the shared test database holds other tests' turns.
    for _ in range(40):
        beat.tick()
        if str(turn_id) in published:
            break

    assert str(turn_id) in published
    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, turn_id)
        assert (turn.status, turn.abandoned_claims) == ("pending", 1)
    assert beat.receipt()["errors"] == []


@pytest.mark.asyncio
async def test_a_timeout_on_a_lapsed_turn_says_so() -> None:
    turn_id = await _lapsed_turn()
    idle = replay_creator.ReconcileBeat(interval_s=3600)

    with pytest.raises(replay_creator.ReplayStalled) as stalled:
        replay_creator.wait_for_turn(
            turn_id,
            settled=replay_creator.TURN_SETTLED,
            timeout_s=0,
            poll_interval_s=0.2,
            beat=idle,
        )

    assert stalled.value.detail["status"] == "planning"
    assert stalled.value.detail["lease_lapsed"] is True
