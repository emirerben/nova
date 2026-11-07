"""#2183 cutover: removing the dedicated `visuals` group must not strand or time out Visuals.

Runs the three queries of docs/runbooks/visuals-group-removal.md VERBATIM (read from the
runbook) against real PostgreSQL, with the real upload route, task body and reaper. Only the
paid provider call and object storage are stubbed (shared harness from
test_pool_asset_stall_bound_postgres.py). Time moves by ageing committed clocks.

Failure modes, written before the runbook and these tests:
  C1  attempts dispatched under the OLD budget (two slots, no lane occupant, 60-min cap) are
      still queued when capacity shrinks to the one worker slot: their durable deadlines are
      never extended, so the reaper times them out
  C2  the cohort query misses an old-budget attempt, or lists a lane-sized one (wrong gate)
  C3  capacity is removed while that cohort is non-empty, or never (gate never empties
      although the dedicated lanes drained it)
  C4  a stopped/killed visuals machine strands a claimed attempt and the reconcile query does
      not name it for recovery
  C5  reconcile mislabels a finished, re-tried, deleted or still-queued attempt, so the
      operator chases a non-problem or skips a real one
  C6  the runbook SQL drifts from what these tests run (the runbook is the source)
"""

# ruff: noqa: F811  (shared fixtures are imported from the stall-bound harness)

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, text

from app.database import sync_session
from app.models import PlanItemAsset
from app.services import pool_analysis_deadline as bound
from app.tasks import maintenance
from tests.integration.test_pool_asset_stall_bound_postgres import (  # noqa: F401
    _DRIVE_IMPORT,
    _HEALTHY_RUN,
    HardKill,
    _add_asset,
    _age,
    _fresh_async_pool,
    _ok_analysis,
    _queue,
    _reanalyze,
    _row,
    _run_task,
    _slow_burst_on_one_lane,
    env,
    owner,
)

RUNBOOK = Path(__file__).resolve().parents[5] / "docs" / "runbooks" / "visuals-group-removal.md"


def _runbook_sql(marker: str) -> str:
    body = RUNBOOK.read_text()
    match = re.search(rf"-- cutover:{re.escape(marker)}\n(.*?;)\n", body, re.S)
    assert match, f"runbook query {marker!r} missing"
    return match.group(1)


def _old_budget_dispatch_deadline(kind, now, backlog_s=0):  # noqa: ANN001
    """The pre-change `dispatch_deadline` exactly: two slots, a 300/720 s slot per
    analysis ahead counted by the NEW file's kind, no occupant, 60-min cap."""
    k = "video" if kind == "video" else "image"
    ahead = int(backlog_s) // bound.queue_slot_s("image")  # this harness queues photos only
    waves = -(-ahead // 2)
    slot = timedelta(seconds=300 if k == "image" else 720)
    base = timedelta(minutes=10 if k == "image" else 30)
    claim = min(max(base, slot * waves + timedelta(minutes=10)), timedelta(minutes=60))
    return now + claim + bound.run_window(k)


async def _dispatch_old_budget_burst(owner, monkeypatch, files: int) -> list[str]:  # noqa: ANN001
    ids = [_add_asset(owner, name=f"old-{i:02d}.jpg") for i in range(files)]
    with monkeypatch.context() as m:
        m.setattr(bound, "dispatch_deadline", _old_budget_dispatch_deadline)
        for aid in ids:
            await _queue(owner, aid)
    return ids


def _cohort(owner, t_a: datetime) -> dict[str, str]:  # noqa: ANN001
    sql = _runbook_sql("old-budget-cohort").replace("<T_A>", t_a.isoformat())
    with sync_session() as db:
        rows = db.execute(text(sql)).mappings().all()
        mine = {
            str(r)
            for r in db.execute(
                text("SELECT id FROM plan_item_assets WHERE user_id = :u"), {"u": owner.user_id}
            ).scalars()
        }
    return {str(r["id"]): r["analysis_attempt_token"] for r in rows if str(r["id"]) in mine}


def _inventory(owner) -> dict[str, str]:  # noqa: ANN001
    with sync_session() as db:
        rows = db.execute(text(_runbook_sql("inventory"))).mappings().all()
    return {
        str(r["id"]): r["analysis_attempt_token"] for r in rows if r["user_id"] == owner.user_id
    }


def _reconcile(pairs: dict[str, str]) -> dict[str, str]:
    values = ", ".join(f"('{aid}'::uuid, '{token}')" for aid, token in pairs.items())
    sql = re.sub(r"VALUES <[^>]*>\)", f"VALUES {values})", _runbook_sql("reconcile"))
    with sync_session() as db:
        rows = db.execute(text(sql)).mappings().all()
    return {str(r["id"]): r["outcome"] for r in rows}


def _drain_on_dedicated_lanes(ids: list[str], lanes: int) -> None:
    """The visuals group (6) plus the worker (1): `lanes` claims per healthy run."""
    for start in range(0, len(ids), lanes):
        for aid in ids[start : start + lanes]:
            row = _row(aid)
            if row.status == "queued":
                _run_task(aid, row.analysis_attempt_token, _ok_analysis(aid))
        for aid in ids:
            _age(aid, _HEALTHY_RUN)
        maintenance.reconcile_stale_pool_assets()


async def test_old_budget_cohort_times_out_if_capacity_shrinks_first(env, owner, monkeypatch):
    # C1 red: the lane-sized code is live (T_A) but these attempts were stamped
    # before it; shrinking to the one worker slot now times some of them out,
    # because nothing extends a durable deadline.
    ids = await _dispatch_old_budget_burst(owner, monkeypatch, files=14)
    t_a = datetime.now(UTC) + timedelta(seconds=1)
    assert set(_cohort(owner, t_a)) == set(ids)  # C2: the gate sees them

    for aid in ids:
        _age(aid, timedelta(seconds=1800))  # a render already on the lane
    maintenance.reconcile_stale_pool_assets()
    for aid in ids:
        row = _row(aid)
        if row.status == "queued":
            _run_task(aid, row.analysis_attempt_token, _ok_analysis(aid))
        for other in ids:
            _age(other, _HEALTHY_RUN)
        maintenance.reconcile_stale_pool_assets()
    codes = [_row(aid).error_code for aid in ids]
    assert bound.TIMED_OUT_CODE in codes, codes


async def test_old_budget_cohort_drains_before_capacity_is_removed(env, owner, monkeypatch):
    # C1-C3 control: the same old-budget burst, still on dedicated capacity
    # while the runbook's cohort query lists it, all ends ready; only then does
    # the gate empty, and a lane-sized burst on the single worker slot (the
    # post-removal state) also ends ready.
    ids = await _dispatch_old_budget_burst(owner, monkeypatch, files=14)
    # T_A: the lane-sized deploy completes; the next dispatch stamps the new budget.
    lane_sized = _add_asset(owner, name="after-t-a.jpg")
    await _queue(owner, lane_sized)
    with sync_session() as db:
        dispatched = db.get(PlanItemAsset, uuid.UUID(lane_sized)).analysis_last_dispatched_at
    t_a = dispatched - timedelta(milliseconds=1)
    cohort = _cohort(owner, t_a)
    assert set(cohort) == set(ids)  # C2: every old attempt, not the lane-sized one
    assert lane_sized not in cohort

    _drain_on_dedicated_lanes(ids, lanes=7)
    assert {_row(aid).status for aid in ids} == {"ready"}
    assert _cohort(owner, t_a) == {}  # C3: the gate opens only now

    # Post-removal: one worker slot, a 40-min Drive import ahead, lane-sized budget.
    outcomes = await _slow_burst_on_one_lane(owner, files=14, occupant=_DRIVE_IMPORT)
    assert {status for status, _code in outcomes.values()} == {"ready"}, outcomes


async def test_reconcile_names_every_inventoried_attempt_outcome(env, owner):
    # C4/C5: inventory before the stop, then: one finishes, one is claimed and
    # killed by the stop, one is killed and the creator retries, one is deleted,
    # one is still waiting on the worker. 20 min later the runbook query must
    # name each correctly.
    names = ("done", "stranded", "retried", "deleted", "waiting")
    ids = {name: _add_asset(owner, name=f"{name}.jpg") for name in names}
    for aid in ids.values():
        await _queue(owner, aid)
    inventory = _inventory(owner)
    assert set(inventory) == set(ids.values())

    def killed(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise HardKill()

    _run_task(ids["done"], inventory[ids["done"]], _ok_analysis("done"))
    for name in ("stranded", "retried"):
        with pytest.raises(HardKill):
            _run_task(ids[name], inventory[ids[name]], killed)
        assert _row(ids[name]).status == "analyzing"
    with sync_session() as db:
        db.execute(delete(PlanItemAsset).where(PlanItemAsset.id == uuid.UUID(ids["deleted"])))
        db.commit()

    for name in ("done", "stranded", "retried", "waiting"):
        _age(ids[name], timedelta(minutes=20))
    maintenance.reconcile_stale_pool_assets()
    await _reanalyze(owner, ids["retried"])

    outcomes = _reconcile(inventory)
    assert outcomes == {
        ids["done"]: "done",
        ids["stranded"]: "needs_retry",
        ids["retried"]: "new_attempt",
        ids["deleted"]: "removed",
        ids["waiting"]: "still_in_flight",
    }
    assert _row(ids["waiting"]).status == "queued"  # lane-sized: 20 min is inside its bound
