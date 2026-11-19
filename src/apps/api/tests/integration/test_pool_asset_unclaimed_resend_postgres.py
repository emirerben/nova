"""KRI-1469 E2E: a Visual whose analysis message nobody claimed is sent again.

Q1 runs 3 and 4 (2026-11-14, 2026-11-17): 10 Visuals uploaded to one chat, 9 `ready` within
seconds, the 10th `queued`, `analysis_attempt_count=1`, no error, for as long as anyone
watched. Nothing re-sent it: the reaper only failed it at its deadline (~1 h for a photo).
On 11-17 the stuck row was the first one created in the batch. Why its broker message was
never claimed is not known (the Fly log had rolled past it both times).

Real PostgreSQL, the real dispatch (`plan_items._queue_pool_asset_analysis`), the real Celery
task body (`analyze_pool_asset.run`) and the real maintenance reaper. Only the paid provider
call and object storage are stubbed; broker publishes are captured.

Ways this can fail, written before the fix:
  R1  the incident: a `queued` row whose message was lost while later Visuals of the same
      item were claimed stays `queued` until its deadline.
  R2  a re-send starts a new attempt (new token / count / deadline): a copy that was only
      delayed is then refused, or the KRI-820 bound is extended.
  R3  the original and the re-sent copy both run paid analysis, or the second overwrites
      the finished row.
  R4  a healthy batch only waiting behind a busy lane is re-sent on every sweep (a broker
      flood): nothing may be re-sent while nothing later was claimed, and a row at most
      once per 10 minutes.
  R5  a row past its deadline is re-sent instead of becoming the retryable timeout.
  R6  a failed re-send publish fails the row or the sweep; the row must stay `queued`
      (its first copy may still be in line) and the next sweep tries again.
  R7  the kill switch off is not the old behaviour (no re-send at all).
  R8  the re-sent copy carries no token header or the wrong queue, so the worker refuses it
      and recovery never happens.

Writes `.dev/kri-1469-e2e/report.json` at the repo root.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import settings
from app.services import pool_analysis_deadline as bound
from app.tasks import maintenance
from app.tasks.autoplace import analyze_pool_asset
from tests.integration import test_pool_asset_stall_bound_postgres as stall
from tests.integration.test_pool_asset_stall_bound_postgres import (
    _add_asset,
    _age,
    _ok_analysis,
    _queue,
    _row,
    _run_task,
)

# The KRI-820 suite's fixtures: real Postgres owner, captured broker publishes.
env = stall.env
owner = stall.owner
_fresh_async_pool = stall._fresh_async_pool

REPORT_PATH = Path(__file__).resolve().parents[5] / ".dev" / "kri-1469-e2e" / "report.json"
REPORT: dict = {"steps": []}

# The incident's batch (Q1 kit). Names only; no media.
BATCH = [
    "Screenshot 5 gamestudios.png",
    "Screenshot 1 dashboard.png",
    "Screenshot 2 repo.png",
    "Screenshot 3 tradingagents.png",
    "Screenshot 4 pricing.png",
    "Recording 1.png",
    "Recording 2.png",
    "Recording 3.png",
    "Recording 4.png",
    "Recording 5.png",
]


def _step(name: str, **evidence) -> None:
    REPORT["steps"].append({"step": name, **json.loads(json.dumps(evidence, default=str))})


@pytest.fixture(scope="module", autouse=True)
def _write_report():
    yield
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT["command"] = (
        "cd src/apps/api && DATABASE_URL=postgresql://localhost/<db>_test "
        "pytest tests/integration/test_pool_asset_unclaimed_resend_postgres.py -v"
    )
    REPORT_PATH.write_text(json.dumps(REPORT, indent=2, sort_keys=True))


@pytest.fixture(autouse=True)
def _resend_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "pool_unclaimed_resend_enabled", True)


def _copies_for(env, asset_id: str) -> list[dict]:  # noqa: ANN001
    return [p for p in env.published if p["args"][0] == asset_id]


async def _batch(env, owner) -> list[str]:  # noqa: ANN001
    """Ten Visuals registered in order, each dispatched by the real route helper."""
    ids = []
    for name in BATCH:
        asset_id = _add_asset(owner, name=name)
        await _queue(owner, asset_id)
        ids.append(asset_id)
    return ids


class _Counter:
    def __init__(self, subject: str) -> None:
        self.calls = 0
        self._analyze = _ok_analysis(subject)

    def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN204
        self.calls += 1
        return self._analyze(*args, **kwargs)


async def test_lost_first_message_is_resent_once_the_rest_of_the_batch_is_claimed(
    env, owner
) -> None:
    """R1, R2, R3, R8: the incident, end to end."""
    ids = await _batch(env, owner)
    lost, rest = ids[0], ids[1:]
    first = _row(lost)
    first_copy = _copies_for(env, lost)[-1]
    for asset_id in rest:  # the worker drains every other message in seconds
        _run_task(
            asset_id, _row(asset_id).analysis_attempt_token, _ok_analysis(_row(asset_id).id.hex)
        )
    assert {_row(a).status for a in rest} == {"ready"}

    # Inside the first two minutes nothing is re-sent: the lane may still be on its way.
    maintenance.reconcile_stale_pool_assets()
    assert len(_copies_for(env, lost)) == 1

    _age(lost, timedelta(minutes=3))
    aged = _row(lost)
    maintenance.reconcile_stale_pool_assets()
    copies = _copies_for(env, lost)
    assert len(copies) == 2
    resent = copies[-1]
    after = _row(lost)
    # Same attempt: token, count and deadline unchanged; the broker drops it at the deadline.
    assert after.status == "queued"
    assert after.analysis_attempt_token == first.analysis_attempt_token
    assert after.analysis_attempt_count == first.analysis_attempt_count == 1
    assert after.analysis_deadline_at == aged.analysis_deadline_at
    assert resent["headers"]["pool_asset_attempt_token"] == first.analysis_attempt_token
    assert resent["queue"] == first_copy["queue"] == settings.pool_asset_visuals_queue
    assert resent["args"] == [lost, False]
    assert resent["expires"] == aged.analysis_deadline_at

    # The worker takes the re-sent copy: the Visual turns ready.
    analysis = _Counter("gamestudios")
    with patch("app.tasks.autoplace._analyze_image", analysis):
        analyze_pool_asset.run(lost, False, resent["headers"]["pool_asset_attempt_token"])
    done = _row(lost)
    assert done.status == "ready" and done.analysis["subject"] == "gamestudios"

    # The first copy turns up late after all: a no-op, no second paid call, nothing rewritten.
    with patch("app.tasks.autoplace._analyze_image", analysis):
        analyze_pool_asset.run(lost, False, first_copy["headers"]["pool_asset_attempt_token"])
    assert analysis.calls == 1
    late = _row(lost)
    assert late.status == "ready" and late.analysis == done.analysis
    assert late.media_status == done.media_status
    assert late.analysis_attempt_token == done.analysis_attempt_token
    _step(
        "incident_batch_lost_first_message",
        visuals=len(ids),
        ready_before_resend=len(rest),
        copies_of_lost=len(copies),
        token_kept=after.analysis_attempt_token == first.analysis_attempt_token,
        deadline_kept=after.analysis_deadline_at == aged.analysis_deadline_at,
        final=[late.status, late.source_filename],
        paid_calls_for_lost=analysis.calls,
    )


async def test_duplicate_copy_after_the_original_claimed_is_a_no_op(env, owner) -> None:
    """R3, other order: the delayed original wins, the re-sent copy finds nothing to do."""
    ids = await _batch(env, owner)
    lost = ids[0]
    for asset_id in ids[1:]:
        _run_task(asset_id, _row(asset_id).analysis_attempt_token, _ok_analysis("other"))
    _age(lost, timedelta(minutes=3))
    maintenance.reconcile_stale_pool_assets()
    original, resent = _copies_for(env, lost)
    analysis = _Counter("delayed original")
    with patch("app.tasks.autoplace._analyze_image", analysis):
        analyze_pool_asset.run(lost, False, original["headers"]["pool_asset_attempt_token"])
        done = _row(lost)
        analyze_pool_asset.run(lost, False, resent["headers"]["pool_asset_attempt_token"])
    assert analysis.calls == 1
    assert _row(lost).analysis == done.analysis and done.status == "ready"
    _step("duplicate_after_original", paid_calls=analysis.calls, final=done.status)


async def test_a_batch_only_waiting_behind_a_busy_lane_is_not_resent(env, owner) -> None:
    """R4: nothing claimed after them, under 10 min: no copy at all; then one per 10 min."""
    ids = await _batch(env, owner)

    def copies() -> list[int]:
        return [len(_copies_for(env, a)) for a in ids]

    for asset_id in ids:
        _age(asset_id, timedelta(minutes=5))
    maintenance.reconcile_stale_pool_assets()
    assert copies() == [1] * len(ids)
    assert {_row(a).status for a in ids} == {"queued"}

    for asset_id in ids:
        _age(asset_id, timedelta(minutes=5, seconds=1))
    maintenance.reconcile_stale_pool_assets()
    assert copies() == [2] * len(ids)  # one more copy each after 10 minutes
    maintenance.reconcile_stale_pool_assets()
    assert copies() == [2] * len(ids)  # the next sweep sends nothing
    for asset_id in ids:
        _age(asset_id, timedelta(minutes=9))
    maintenance.reconcile_stale_pool_assets()
    assert copies() == [2] * len(ids)  # still inside 10 min of the copy
    for asset_id in ids:
        _age(asset_id, timedelta(minutes=1, seconds=1))
    maintenance.reconcile_stale_pool_assets()
    assert copies() == [3] * len(ids)
    assert {_row(a).analysis_attempt_count for a in ids} == {1}
    _step(
        "busy_lane_bounded",
        rows=len(ids),
        copies_at_5_min=0,
        copies_per_row_at_10_min=1,
        copies_per_row_at_20_min=2,
    )


async def test_a_lone_lost_photo_is_resent_after_ten_minutes(env, owner) -> None:
    """R1 without siblings: a single upload whose message was lost."""
    lone = _add_asset(owner, name="lone.png")
    await _queue(owner, lone)
    _age(lone, timedelta(minutes=9))
    maintenance.reconcile_stale_pool_assets()
    assert len(_copies_for(env, lone)) == 1
    _age(lone, timedelta(minutes=1, seconds=1))
    maintenance.reconcile_stale_pool_assets()
    assert len(_copies_for(env, lone)) == 2
    _run_task(lone, _row(lone).analysis_attempt_token, _ok_analysis("lone"))
    assert _row(lone).status == "ready"
    _step("lone_lost_photo", copies=2, final="ready")


async def test_a_row_past_its_deadline_times_out_and_is_never_resent(env, owner) -> None:
    """R5: KRI-820 is unchanged; the deadline still ends a row nobody ever claims."""
    lost = _add_asset(owner, name="lost.png")
    await _queue(owner, lost)
    queued = _row(lost)
    _age(lost, queued.analysis_deadline_at - datetime.now(UTC) + timedelta(seconds=1))
    copies_before = len(_copies_for(env, lost))
    maintenance.reconcile_stale_pool_assets()
    row = _row(lost)
    assert (row.status, row.error_code, row.error_retryable) == (
        "failed",
        bound.TIMED_OUT_CODE,
        True,
    )
    assert len(_copies_for(env, lost)) == copies_before
    _step("past_deadline_times_out", final=[row.status, row.error_code])


async def test_a_failed_resend_leaves_the_row_queued_for_the_next_sweep(env, owner) -> None:
    """R6."""
    lost = _add_asset(owner, name="lost.png")
    await _queue(owner, lost)
    _age(lost, timedelta(minutes=11))
    with patch.object(analyze_pool_asset, "apply_async", side_effect=ConnectionError("broker")):
        maintenance.reconcile_stale_pool_assets()
    row = _row(lost)
    assert row.status == "queued" and row.error_code is None
    _age(lost, timedelta(minutes=11))
    maintenance.reconcile_stale_pool_assets()
    assert len(_copies_for(env, lost)) == 2
    _step("failed_resend_retried", final=_row(lost).status)


async def test_kill_switch_off_is_the_old_behaviour(env, owner, monkeypatch) -> None:
    """R7: the incident with the switch off: the row waits for its deadline, no copy."""
    monkeypatch.setattr(settings, "pool_unclaimed_resend_enabled", False)
    ids = await _batch(env, owner)
    for asset_id in ids[1:]:
        _run_task(asset_id, _row(asset_id).analysis_attempt_token, _ok_analysis("other"))
    _age(ids[0], timedelta(minutes=30))
    maintenance.reconcile_stale_pool_assets()
    assert len(_copies_for(env, ids[0])) == 1
    assert _row(ids[0]).status == "queued"
    _step("kill_switch_off", copies=1, final="queued")
