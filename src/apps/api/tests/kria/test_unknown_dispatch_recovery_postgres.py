"""Real row locks and query budgets for unknown cloud-dispatch recovery."""

import threading
import uuid

import pytest
from sqlalchemy import event, select

from app.config import settings
from app.database import sync_engine, sync_session
from app.models import (
    ContentPlan,
    CreationThreadEvent,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    PlanItem,
)
from app.tasks import kria_runtime as rt
from tests.kria.test_render_card_kick_postgres import _seed_dispatched_render

# These cases deliberately observe 100 receipts / contend on real database locks;
# the repository's 30-second unit-test cap is not a product latency assertion.
pytestmark = pytest.mark.timeout(120)


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)

    def forbidden(*_args, **_kwargs):
        pytest.fail("unknown-receipt observation must not dispatch work")

    monkeypatch.setattr(rt.run_kria_turn, "apply_async", forbidden)
    monkeypatch.setattr(rt.execute_kria_approval, "apply_async", forbidden)
    monkeypatch.setattr(rt, "enqueue_editor_commit_render", forbidden)
    monkeypatch.setattr(rt, "_promote_queued_successor_sync", forbidden)


def seed_unknown(*, count=1):
    seeded = _seed_dispatched_render(
        job_status="variants_ready",
        assembly_plan={
            "variants": [
                {
                    "variant_id": "cut",
                    "render_generation_id": "new",
                    "render_status": "ready",
                    "video_path": "test/result.mp4",
                }
            ]
        },
    )
    error = {"code": "render_dispatch_outcome_unknown", "retryable": False, "recovery": "manual"}
    with sync_session() as db:
        execution = db.get(CreatorAgentExecution, seeded.execution_id)
        execution.status = "outcome_unknown"
        execution.dispatched_at = None
        execution.error = error
        execution.target_variant_id = "cut"
        execution.target_generation_id = "new"
        execution.result = {"editor_prep": {"render_destination": "cloud"}}
        session = db.get(CreatorAgentSession, seeded.session_id)
        session.status = "awaiting_feedback"
        session.last_error = error
        turn = db.get(CreatorAgentTurn, seeded.turn_id)
        turn.status = "failed"
        turn.error = error
        receipts = [execution]
        for _ in range(count - 1):
            row = CreatorAgentExecution(
                session_id=seeded.session_id,
                turn_id=seeded.turn_id,
                idempotency_key=uuid.uuid4().hex,
                request_digest=uuid.uuid4().hex,
                expected_revision=0,
                status="outcome_unknown",
                error=error,
                target_thread_id=seeded.thread_id,
                target_job_id=seeded.job_id,
                target_variant_id="cut",
                target_generation_id="new",
                result={"editor_prep": {"render_destination": "cloud"}},
            )
            db.add(row)
            receipts.append(row)
        db.flush()
        receipts.sort(key=lambda row: row.id)
        for row in receipts[:-1]:
            row.target_generation_id = f"unconfirmed-{row.id}"
        seeded.execution_id = receipts[-1].id
        db.commit()
    return seeded


def cards(thread_id):
    with sync_session() as db:
        return list(
            db.scalars(
                select(CreationThreadEvent).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type.in_({"generation_ready", "assistant_review"}),
                )
            )
        )


def test_postgres_rotates_past_fifty_unknowns_and_completes_exact_receipt_once():
    seeded = seed_unknown(count=51)
    assert rt.observe_kria_job.run(str(seeded.job_id)) == {"settled": 0}
    with sync_session() as db:
        rows = list(
            db.scalars(
                select(CreatorAgentExecution).where(
                    CreatorAgentExecution.target_job_id == seeded.job_id
                )
            )
        )
        assert sum(row.observed_at is not None for row in rows) == 50
        assert all(row.status == "outcome_unknown" for row in rows)
    assert cards(seeded.thread_id) == []
    assert rt.observe_kria_job.run(str(seeded.job_id)) == {"settled": 1}
    assert rt._observe_dispatched_execution(seeded.execution_id) == ("ignored", None)
    assert sorted(card.event_type for card in cards(seeded.thread_id)) == [
        "assistant_review",
        "generation_ready",
    ]
    with sync_session() as db:
        assert db.get(CreatorAgentExecution, seeded.execution_id).status == "completed"
        assert db.get(CreatorAgentTurn, seeded.turn_id).status == "completed"
        assert (
            db.get(CreatorAgentSession, seeded.session_id).last_good["render_generation_id"]
            == "new"
        )


def test_postgres_racing_unknown_observers_complete_once():
    seeded = seed_unknown()
    with sync_session() as db:
        plan_id = db.get(PlanItem, seeded.plan_item_id).content_plan_id
    results, errors = [], []
    arrivals = set()
    reached = threading.Event()
    guard = threading.Lock()

    def observe():
        try:
            results.append(rt.observe_kria_job.run(str(seeded.job_id)))
        except BaseException as exc:
            errors.append(exc)
        finally:
            with guard:
                if len(results) + len(errors) == 2:
                    reached.set()

    threads = [threading.Thread(target=observe) for _ in range(2)]

    def before_lock(_conn, _cursor, statement, _parameters, _context, _executemany):
        if (
            threading.current_thread() in threads
            and "FROM content_plans" in statement
            and "FOR " in statement
        ):
            with guard:
                arrivals.add(threading.get_ident())
                if len(arrivals) == 2:
                    reached.set()

    event.listen(sync_engine, "before_cursor_execute", before_lock)
    try:
        with sync_engine.connect() as holder:
            holder.execute(
                select(ContentPlan.id).where(ContentPlan.id == plan_id).with_for_update()
            ).all()
            for thread in threads:
                thread.start()
            try:
                assert reached.wait(60), "integration workers did not reach observer lock boundary"
                assert len(arrivals) == 2, f"observers exited before locking: {results}, {errors}"
            finally:
                holder.rollback()
                for thread in threads:
                    thread.join(timeout=15)
    finally:
        event.remove(sync_engine, "before_cursor_execute", before_lock)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(result["settled"] for result in results) == [0, 1]
    assert sorted(card.event_type for card in cards(seeded.thread_id)) == [
        "assistant_review",
        "generation_ready",
    ]
