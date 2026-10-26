"""KRI-820 E2E: a stalled Visuals analysis ends within its declared bound.

Real PostgreSQL, the real routes (list / reanalyze / delete), the real Celery
task body (`analyze_pool_asset.run`), the real maintenance reaper and the real
creator-preparation source pass. Only the paid provider call and object storage
are stubbed; the broker publish is captured so its `expires` can be asserted.

Time travel is done the way a wall clock would move it: committed row
timestamps are shifted into the past, so every reader (projection, retry
route, reaper, creator preparation) sees the same elapsed time.

Failure modes covered (written before the fix, plan.md F1-F14):
  F1/F7  hard-killed claim -> failed/analysis_timed_out/retryable on read and
         after the reaper, unchanged on a fresh session (reopen)
  F2/F6  reaper never extends the deadline, never loops past it
  F3/F4/F5 retry of an expired row mints ONE new token (concurrent taps), the
         old worker's late completion cannot write, the new attempt wins
  F9     creator preparation proceeds with ready sources and names the stalled one
  F10    removing the stalled asset leaves no row for a late worker to write
  F12    refresh of a ready asset never degrades it
  F13    another creator cannot retry the asset
  F14    media-library re-dispatch is capped
  F16    broker `expires` equals the row's deadline
  F17    healthy photos only WAITING in a shared-queue backlog never time out:
         the claim window grows with the live backlog (capped at the old
         60-minute queued window); the run window after a claim stays tight
  F18    a timeout the reaper already persisted is omitted by creator
         preparation without a second paid inline analysis

Writes `.dev/kri-820-e2e/report.json` at the repo root.
"""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import AsyncSessionLocal, sync_engine, sync_session
from app.database import engine as async_engine
from app.models import (
    ContentPlan,
    CreatorAgentEvent,
    CreatorAgentSession,
    CreatorPlanningAttempt,
    MediaAsset,
    Persona,
    PlanItem,
    PlanItemAsset,
    User,
)
from app.routes import plan_items
from app.services import pool_analysis_deadline as bound
from app.services.creator_preparation import progress, source_digest, source_snapshot
from app.tasks import creator_preparation as prep
from app.tasks import maintenance, media_library
from app.tasks.autoplace import analyze_pool_asset

_database_name = make_url(settings.database_url).database or ""
if not _database_name.endswith("_test"):
    pytest.skip(f"refusing to write fixtures to {_database_name!r}", allow_module_level=True)
try:
    with sync_engine.connect() as _probe:
        _probe.execute(text("SELECT analysis_deadline_at FROM plan_item_assets LIMIT 0"))
except OperationalError as exc:  # pragma: no cover - environment guard
    pytest.skip(f"test PostgreSQL unavailable: {exc!r}", allow_module_level=True)

REPORT_PATH = Path(__file__).resolve().parents[5] / ".dev" / "kri-820-e2e" / "report.json"
REPORT: dict = {"steps": []}


class HardKill(BaseException):  # noqa: N818 - models SIGKILL, not an error class
    """Escapes every `except Exception`, like a killed worker: no persist runs."""


def _jpeg_bytes() -> bytes:
    from PIL import Image  # noqa: PLC0415

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 120, 40)).save(buf, "JPEG")
    return buf.getvalue()


_JPEG = _jpeg_bytes()


def _download(path, local, **_kwargs):  # noqa: ANN001
    Path(local).write_bytes(_JPEG)


def _ok_analysis(subject: str):
    def analyze(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return {"subject": subject, "description": subject}, 64 / 48, (64, 48), False

    return analyze


def _step(name: str, **evidence) -> None:
    REPORT["steps"].append({"step": name, **json.loads(json.dumps(evidence, default=str))})


@pytest.fixture(scope="module", autouse=True)
def _write_report():
    yield
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT["command"] = (
        "cd src/apps/api && DATABASE_URL=postgresql://localhost/<db>_test "
        "pytest tests/integration/test_pool_asset_stall_bound_postgres.py -v"
    )
    REPORT["declared_bound_s"] = {
        kind: {
            "claim_window": bound.claim_window(kind).total_seconds(),
            "run_window": bound.run_window(kind).total_seconds(),
        }
        for kind in ("image", "video")
    }
    REPORT_PATH.write_text(json.dumps(REPORT, indent=2, sort_keys=True))


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch):
    published: list[dict] = []
    monkeypatch.setattr(settings, "overlay_autoplace_enabled", True)
    monkeypatch.setattr(settings, "pool_asset_queued_status_enabled", True)
    monkeypatch.setattr(settings, "media_library_enabled", False)
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                analyze_pool_asset,
                "apply_async",
                side_effect=lambda **kw: published.append(kw) or SimpleNamespace(id="receipt"),
            )
        )
        stack.enter_context(patch("app.storage.download_generation_to_file", _download))
        stack.enter_context(patch("app.storage.download_to_file", _download))
        stack.enter_context(
            patch("app.routes.plan_items.storage.signed_get_url", return_value="https://signed")
        )
        for name in ("delete_object_generation_best_effort", "delete_object_best_effort"):
            stack.enter_context(patch(f"app.storage.{name}", return_value=True))
        stack.enter_context(
            patch("app.tasks.autoplace.generate_pool_asset_preview.apply_async", return_value=None)
        )
        yield SimpleNamespace(published=published)


def _seed_owner(label: str) -> SimpleNamespace:
    uid, pid, iid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with sync_session() as db:
        db.add(User(id=uid, email=f"kri820-{label}-{uid}@example.test"))
        db.flush()
        persona = Persona(user_id=uid, persona_status="ready", questionnaire={}, persona={})
        db.add(persona)
        db.flush()
        db.add(ContentPlan(id=pid, user_id=uid, persona_id=persona.id, plan_status="ready"))
        db.flush()
        db.add(
            PlanItem(
                id=iid,
                content_plan_id=pid,
                position=1,
                idea="Weekend slider",
                edit_format="slide_post",
                item_status="awaiting_clips",
                clip_gcs_paths=[],
            )
        )
        db.commit()
    return SimpleNamespace(user_id=uid, plan_id=pid, item_id=iid, user=SimpleNamespace(id=uid))


def _drop_owner(owner: SimpleNamespace) -> None:
    with sync_session() as db:
        db.execute(delete(MediaAsset).where(MediaAsset.user_id == owner.user_id))
        db.execute(delete(ContentPlan).where(ContentPlan.id == owner.plan_id))
        db.execute(delete(Persona).where(Persona.user_id == owner.user_id))
        db.execute(delete(User).where(User.id == owner.user_id))
        db.commit()


@pytest.fixture()
def owner():
    value = _seed_owner("owner")
    yield value
    _drop_owner(value)


@pytest.fixture()
def stranger():
    value = _seed_owner("stranger")
    yield value
    _drop_owner(value)


def _add_asset(owner, *, status: str = "uploaded", name: str = "photo.jpg", **fields) -> str:
    aid = uuid.uuid4()
    with sync_session() as db:
        db.add(
            PlanItemAsset(
                id=aid,
                plan_item_id=owner.item_id,
                user_id=owner.user_id,
                kind=fields.pop("kind", "image"),
                gcs_path=f"users/{owner.user_id}/plan/{owner.item_id}/pool/{aid}.jpg",
                gcs_generation=fields.pop("gcs_generation", "1111"),
                upload_content_type="image/jpeg",
                source_filename=name,
                status=status,
                **fields,
            )
        )
        db.commit()
    return str(aid)


def _row(asset_id: str) -> PlanItemAsset | None:
    with sync_session() as db:
        row = db.get(PlanItemAsset, uuid.UUID(asset_id))
        if row is not None:
            db.expunge(row)
        return row


def _age(asset_id: str, by: timedelta) -> None:
    """Move every committed clock on the row into the past (time travel)."""
    with sync_session() as db:
        row = db.get(PlanItemAsset, uuid.UUID(asset_id), with_for_update=True)
        for column in (
            "analysis_deadline_at",
            "analysis_started_at",
            "analysis_last_dispatched_at",
            "created_at",
        ):
            value = getattr(row, column)
            if value is not None:
                setattr(row, column, value - by)
        db.commit()


async def _queue(owner, asset_id: str) -> None:
    async with AsyncSessionLocal() as db:
        row = await db.get(PlanItemAsset, uuid.UUID(asset_id))
        await plan_items._queue_pool_asset_analysis(row, db, reset_attempts=True)


async def _list(owner) -> dict[str, dict]:
    async with AsyncSessionLocal() as db:
        out = await plan_items.list_pool_assets(str(owner.item_id), user=owner.user, db=db)
    return {a.id: a.model_dump(mode="json") for a in out.assets}


async def _reanalyze(owner, asset_id: str) -> dict:
    async with AsyncSessionLocal() as db:
        out = await plan_items.reanalyze_pool_asset(
            str(owner.item_id), asset_id, user=owner.user, db=db
        )
    return out.model_dump(mode="json")


def _run_task(asset_id: str, token: str, analyze) -> None:  # noqa: ANN001
    with patch("app.tasks.autoplace._analyze_image", analyze):
        analyze_pool_asset.run(asset_id, False, token)


@pytest.fixture(autouse=True)
async def _fresh_async_pool():
    await async_engine.dispose()
    yield
    await async_engine.dispose()


async def test_hard_killed_claim_becomes_actionable_failure_within_bound(env, owner) -> None:
    asset_id = _add_asset(owner)
    t0 = datetime.now(UTC)
    with sync_session() as db:
        backlog = int(db.execute(bound.queue_backlog_stmt(t0)).scalar() or 0)
    await _queue(owner, asset_id)
    queued = _row(asset_id)
    publish = env.published[-1]
    # F16: a broker copy older than the deadline is dropped by the worker.
    assert queued.analysis_deadline_at is not None
    assert publish["expires"] == queued.analysis_deadline_at
    assert queued.analysis_deadline_at - t0 <= bound.claim_window(
        "image", backlog
    ) + bound.run_window("image") + timedelta(seconds=5)

    def killed(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise HardKill()

    with pytest.raises(HardKill):
        _run_task(asset_id, queued.analysis_attempt_token, killed)
    claimed = _row(asset_id)
    assert claimed.status == "analyzing"
    # Claim shortens the bound to one run from the claim.
    assert claimed.analysis_deadline_at <= claimed.analysis_started_at + bound.run_window("image")
    listed = (await _list(owner))[asset_id]
    assert listed["status"] == "analyzing"
    assert listed["analysis_deadline_at"] is not None

    _age(asset_id, bound.run_window("image") + timedelta(seconds=1))
    projected = (await _list(owner))[asset_id]
    assert (projected["status"], projected["error_code"], projected["retryable"]) == (
        "failed",
        "analysis_timed_out",
        True,
    )

    maintenance.reconcile_stale_pool_assets()
    persisted = _row(asset_id)
    assert persisted.status == "failed"
    assert persisted.error_code == "analysis_timed_out"
    assert persisted.error_retryable is True
    assert persisted.analysis_attempt_token != queued.analysis_attempt_token
    reopened = (await _list(owner))[asset_id]
    assert reopened["status"] == "failed" and reopened["error_code"] == "analysis_timed_out"
    _step(
        "hard_killed_claim",
        asset_id=asset_id,
        gcs_generation=queued.gcs_generation,
        broker_expires=publish["expires"],
        deadline_at_dispatch=queued.analysis_deadline_at,
        deadline_at_claim=claimed.analysis_deadline_at,
        projection_after_bound=projected["status"],
        persisted=[persisted.status, persisted.error_code, persisted.error_retryable],
        reopened=[reopened["status"], reopened["error_code"]],
        tokens=[queued.analysis_attempt_token, persisted.analysis_attempt_token],
    )


async def test_reaper_never_extends_or_loops_past_the_deadline(env, owner) -> None:
    asset_id = _add_asset(owner)
    await _queue(owner, asset_id)
    queued = _row(asset_id)
    publishes_before = len(env.published)
    window = queued.analysis_deadline_at - queued.analysis_last_dispatched_at
    timeline = []
    for minutes in (5, 5, 5):  # message lost: never claimed, sweeps every 5 min
        _age(asset_id, timedelta(minutes=minutes))
        maintenance.reconcile_stale_pool_assets()
        row = _row(asset_id)
        timeline.append([row.status, row.analysis_deadline_at, row.analysis_attempt_count])
    row = _row(asset_id)
    # Same deadline (shifted only by our time travel), same attempt, no new paid work.
    assert row.status == "queued"
    assert row.analysis_attempt_token == queued.analysis_attempt_token
    assert row.analysis_attempt_count == queued.analysis_attempt_count
    assert len(env.published) == publishes_before
    _age(asset_id, window - timedelta(minutes=15) + timedelta(seconds=1))
    for _ in range(3):
        maintenance.reconcile_stale_pool_assets()
    row = _row(asset_id)
    assert (row.status, row.error_code) == ("failed", "analysis_timed_out")
    assert row.analysis_attempt_count == queued.analysis_attempt_count
    assert len(env.published) == publishes_before
    _step(
        "reaper_bounded",
        asset_id=asset_id,
        timeline=timeline,
        final=[row.status, row.error_code, row.analysis_attempt_count],
        republished=len(env.published) - publishes_before,
    )


async def test_queue_backlog_widens_the_claim_window_not_the_run_window(env, owner) -> None:
    # Two creators' 20-photo batches already in line ahead of this one.
    for i in range(40):
        _add_asset(
            owner,
            name=f"line-{i}.jpg",
            status="queued",
            analysis_attempt_token=f"line-{i}",
            analysis_deadline_at=datetime.now(UTC) + timedelta(hours=1),
        )
    last = _add_asset(owner, name="last-in-line.jpg")
    t0 = datetime.now(UTC)
    await _queue(owner, last)
    queued = _row(last)
    claim = queued.analysis_deadline_at - t0 - bound.run_window("image")
    # 40 ahead at concurrency 2 = 20 waves of a 300 s worst-case run, capped at 60 min.
    assert claim >= bound.claim_window("image", 40) - timedelta(seconds=5)
    assert claim > bound.claim_window("image")
    assert bound.claim_window("image", 40) == timedelta(minutes=60)
    assert bound.claim_window("image", 4) == timedelta(minutes=20)
    assert bound.claim_window("image", 10_000) == timedelta(minutes=60)

    # Still waiting in line 30 min later: neither the read nor the reaper fails it.
    _age(last, timedelta(minutes=30))
    maintenance.reconcile_stale_pool_assets()
    waiting = _row(last)
    assert waiting.status == "queued"
    assert (await _list(owner))[last]["status"] == "queued"

    # Claimed at last: the bound tightens to one run and still ends in time.
    _run_task(last, waiting.analysis_attempt_token, _ok_analysis("beach"))
    done = _row(last)
    assert done.status == "ready"
    _step(
        "queue_backlog_claim_window",
        queue_ahead=40,
        claim_window_s=claim.total_seconds(),
        run_window_s=bound.run_window("image").total_seconds(),
        waited_min=30,
        final=done.status,
    )


async def test_retry_mints_one_token_and_fences_the_late_worker(env, owner) -> None:
    asset_id = _add_asset(owner)
    await _queue(owner, asset_id)
    first = _row(asset_id)
    started = threading.Event()
    release = threading.Event()

    def stuck(*_args, **_kwargs):  # noqa: ANN002, ANN003
        started.set()
        release.wait(30)
        return _ok_analysis("late old attempt")()

    worker = threading.Thread(
        target=_run_task, args=(asset_id, first.analysis_attempt_token, stuck), daemon=True
    )
    worker.start()
    assert started.wait(30)
    # Inside the bound the retry is idempotent: no new attempt.
    inside = await _reanalyze(owner, asset_id)
    assert inside["status"] == "analyzing"
    assert _row(asset_id).analysis_attempt_token == first.analysis_attempt_token

    _age(asset_id, bound.run_window("image") + timedelta(seconds=1))
    publishes_before = len(env.published)
    taps = await asyncio.gather(*(_reanalyze(owner, asset_id) for _ in range(3)))
    retried = _row(asset_id)
    assert {t["status"] for t in taps} == {"queued"}
    assert retried.analysis_attempt_token != first.analysis_attempt_token
    assert len(env.published) == publishes_before + 1  # three taps, one paid attempt
    assert env.published[-1]["headers"]["pool_asset_attempt_token"] == (
        retried.analysis_attempt_token
    )

    release.set()
    worker.join(30)
    after_late = _row(asset_id)
    assert after_late.status == "queued"
    assert after_late.analysis_attempt_token == retried.analysis_attempt_token
    assert after_late.analysis is None

    _run_task(asset_id, retried.analysis_attempt_token, _ok_analysis("fresh retry"))
    done = _row(asset_id)
    assert done.status == "ready" and done.analysis["subject"] == "fresh retry"
    _step(
        "retry_fences_late_worker",
        asset_id=asset_id,
        tokens=[first.analysis_attempt_token, retried.analysis_attempt_token],
        concurrent_taps=len(taps),
        publishes_for_taps=len(env.published) - publishes_before,
        late_worker_wrote=after_late.analysis is not None,
        final=[done.status, done.analysis["subject"]],
    )


async def test_remove_and_ownership_controls(env, owner, stranger) -> None:
    asset_id = _add_asset(owner)
    await _queue(owner, asset_id)
    token = _row(asset_id).analysis_attempt_token
    _age(asset_id, timedelta(hours=1))
    with pytest.raises(HTTPException) as foreign:
        async with AsyncSessionLocal() as db:
            await plan_items.reanalyze_pool_asset(
                str(owner.item_id), asset_id, user=stranger.user, db=db
            )
    assert foreign.value.status_code == 404
    assert _row(asset_id).analysis_attempt_token == token

    async with AsyncSessionLocal() as db:
        await plan_items.delete_pool_asset(str(owner.item_id), asset_id, user=owner.user, db=db)
    _run_task(asset_id, token, _ok_analysis("late after remove"))
    assert _row(asset_id) is None
    _step("remove_and_ownership", foreign_retry_status=404, row_after_late_worker=None)


async def test_refresh_never_degrades_a_ready_asset(env, owner) -> None:
    asset_id = _add_asset(
        owner,
        status="ready",
        media_status="ready",
        analysis={"subject": "kept"},
        analysis_deadline_at=datetime.now(UTC) - timedelta(hours=2),
    )

    def broken(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("provider down")

    with patch("app.tasks.autoplace._analyze_image", broken):
        analyze_pool_asset.run(asset_id, True, None)
    maintenance.reconcile_stale_pool_assets()
    row = _row(asset_id)
    listed = (await _list(owner))[asset_id]
    assert (row.status, row.analysis["subject"], listed["status"]) == ("ready", "kept", "ready")
    _step("refresh_keeps_ready", asset_id=asset_id, final=[row.status, listed["status"]])


def _prep_graph(owner, asset_ids: list[str]) -> uuid.UUID:
    sid, aid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with sync_session() as db:
        item = db.get(PlanItem, owner.item_id)
        assets = list(
            db.execute(
                select(PlanItemAsset)
                .where(PlanItemAsset.id.in_([uuid.UUID(a) for a in asset_ids]))
                .order_by(PlanItemAsset.created_at, PlanItemAsset.id)
            ).scalars()
        )
        db.add(
            CreatorAgentSession(
                id=sid,
                creator_id=owner.user_id,
                plan_item_id=owner.item_id,
                status="planning",
                revision=1,
                ownership_epoch=0,
                preparation=progress(aid, "queued", 0, len(assets)),
            )
        )
        db.flush()
        db.add(
            CreatorAgentEvent(
                id=eid,
                session_id=sid,
                sequence=0,
                revision=1,
                role="user",
                event_type="user_message",
                payload={"message": "Make a slider from my weekend photos"},
            )
        )
        db.flush()
        db.add(
            CreatorPlanningAttempt(
                id=aid,
                session_id=sid,
                plan_item_id=owner.item_id,
                creator_id=owner.user_id,
                source_event_id=eid,
                ownership_epoch=0,
                session_revision=1,
                source_digest=source_digest(source_snapshot(item, assets)),
                inputs={"user_message": "Make a slider from my weekend photos"},
                status="queued",
                attempts=0,
            )
        )
        db.commit()
    return aid


def test_creator_preparation_uses_ready_sources_and_names_the_stalled_one(env, owner) -> None:
    ready = [
        _add_asset(
            owner,
            name=f"ready-{i}.jpg",
            status="ready",
            media_status="ready",
            analysis={"subject": f"ready {i}"},
            gcs_generation=f"20{i}",
        )
        for i in range(2)
    ]
    stalled = _add_asset(owner, name="stalled.jpg", status="analyzing", gcs_generation="303")
    with sync_session() as db:
        db.execute(
            update(PlanItemAsset)
            .where(PlanItemAsset.id == uuid.UUID(stalled))
            .values(
                analysis_attempt_token="stalled-token",
                analysis_started_at=datetime.now(UTC) - timedelta(minutes=9),
                analysis_deadline_at=datetime.now(UTC) - timedelta(minutes=3),
            )
        )
        db.commit()
    persisted_timeout = _add_asset(
        owner,
        name="reaped.jpg",
        status="failed",
        media_status="failed",
        error_code="analysis_timed_out",
        gcs_generation="404",
    )
    broken = _add_asset(
        owner, name="broken.jpg", status="failed", media_status="failed", gcs_generation="505"
    )
    attempt_id = _prep_graph(owner, [*ready, stalled, persisted_timeout, broken])
    token, _inputs, sources, *_ = prep._claim(attempt_id)
    analyzed_ids: list[str] = []

    def analyze(raw, pool, *, run_context, require_semantic):  # noqa: ANN001
        analyzed_ids.append(raw["asset_id"])
        if raw["asset_id"] == broken:
            raise RuntimeError("provider could not read it")
        return {**raw, "generation": raw["storage_generation"]}, None

    with (
        patch.object(prep, "analyze_clip_assignment", analyze),
        patch.object(prep, "clip_analysis_ready", lambda raw, kind=None: bool(raw.get("analysis"))),
    ):
        omitted = prep._analyze_sources(
            attempt_id, token, sources, prep.RunContext(creator_id=str(owner.user_id))
        )
    # The stalled source is never sent for paid analysis; the failed one is tried once.
    assert sorted(analyzed_ids) == sorted([*ready, broken])
    assert omitted == [
        {"asset_id": stalled, "reason": "analysis_timed_out"},
        {"asset_id": persisted_timeout, "reason": "analysis_timed_out"},
        {"asset_id": broken, "reason": "analysis_failed"},
    ]
    with sync_session() as db:
        assert db.get(PlanItemAsset, uuid.UUID(broken)).status == "failed"
    state = progress(attempt_id, "resolving", 2, 4, omitted=omitted)
    assert state["omitted"] == omitted and "left out 3 files" in state["message"]
    _step(
        "creator_preparation_omits_stalled",
        request="Make a slider from my weekend photos",
        ready_sources=ready,
        omitted=omitted,
        resolving_message=state["message"],
    )


def test_creator_preparation_without_any_usable_source_still_fails(env, owner) -> None:
    stalled = _add_asset(owner, status="analyzing", gcs_generation="404")
    with sync_session() as db:
        db.execute(
            update(PlanItemAsset)
            .where(PlanItemAsset.id == uuid.UUID(stalled))
            .values(
                analysis_attempt_token="t",
                analysis_deadline_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        db.commit()
    attempt_id = _prep_graph(owner, [stalled])
    token, _inputs, sources, *_ = prep._claim(attempt_id)
    with pytest.raises(RuntimeError):
        prep._analyze_sources(
            attempt_id, token, sources, prep.RunContext(creator_id=str(owner.user_id))
        )


def test_media_library_redispatch_is_capped(env, owner, monkeypatch) -> None:
    monkeypatch.setattr(settings, "media_library_enabled", True)
    asset_id = uuid.uuid4()
    with sync_session() as db:
        db.add(MediaAsset(id=asset_id, user_id=owner.user_id, kind="image", status="processing"))
        db.commit()
    sent: list[str] = []
    monkeypatch.setattr(
        media_library.analyze_media_asset_task,
        "delay",
        # Count only this asset: rows left by earlier runs share the test DB.
        lambda aid, _owner: sent.append(aid) if aid == str(asset_id) else None,
    )
    statuses = []
    for _ in range(media_library.MEDIA_ANALYSIS_MAX_REDISPATCHES + 2):
        with sync_session() as db:
            db.execute(
                update(MediaAsset)
                .where(MediaAsset.id == asset_id)
                .values(updated_at=datetime.now(UTC) - timedelta(hours=1))
            )
            db.commit()
        media_library.reconcile_media_library_analysis()
        with sync_session() as db:
            statuses.append(db.get(MediaAsset, asset_id).status)
    assert len(sent) == media_library.MEDIA_ANALYSIS_MAX_REDISPATCHES
    assert statuses[-1] == "failed"
    _step("media_library_cap", redispatches=len(sent), statuses=statuses)


def test_report_has_wall_clock(env) -> None:
    REPORT["generated_at"] = datetime.now(UTC).isoformat()
    REPORT["generated_monotonic"] = time.monotonic()
