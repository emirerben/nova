"""Tests for app/services/job_dispatch.py.

Two layers:
  1. Behavior: enqueue_orchestrator sets task_id=str(job_id) on apply_async
     AND persists celery_task_id on the Job row.
  2. Source-grep regression: every orchestrator task dispatch in
     app/routes/ MUST route through the helper or set task_id= explicitly.
     If a new route ever calls orchestrate_X.delay(...) or
     orchestrate_X.apply_async(...) without going through this helper,
     this test fails — preventing silent drift back to "no DB → Celery
     mapping".
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.job_dispatch import (
    ORCHESTRATOR_TASK_NAMES,
    enqueue_orchestrator,
    enqueue_orchestrator_sync,
)

API_ROOT = Path(__file__).resolve().parents[2] / "app"


@pytest.mark.asyncio
async def test_enqueue_orchestrator_sets_task_id_and_persists_column() -> None:
    """apply_async must receive task_id=str(job_id); DB must record celery_task_id."""
    job_id = uuid.uuid4()
    task = MagicMock()
    task.name = "orchestrate_template_job"

    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = "queued"
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[status_result, MagicMock()])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    returned = await enqueue_orchestrator(task, job_id, db)

    assert returned == str(job_id)
    task.apply_async.assert_called_once()
    call_kwargs = task.apply_async.call_args.kwargs
    assert call_kwargs["task_id"] == str(job_id)
    assert call_kwargs["args"] == [str(job_id)]
    assert call_kwargs["kwargs"] == {}
    # UPDATE jobs SET celery_task_id was issued.
    assert db.execute.await_count == 2
    stmt = db.execute.await_args_list[-1].args[0]
    assert "jobs.status !=" in str(stmt)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_orchestrator_forwards_kwargs() -> None:
    """kwargs flow through to apply_async (preview-mode test jobs need this)."""
    job_id = uuid.uuid4()
    task = MagicMock()
    task.name = "orchestrate_template_job"
    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = "queued"
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[status_result, MagicMock()])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    await enqueue_orchestrator(task, job_id, db, kwargs={"force_single_pass": True})

    assert task.apply_async.call_args.kwargs["kwargs"] == {"force_single_pass": True}


@pytest.mark.asyncio
async def test_enqueue_orchestrator_does_not_publish_cancelled_job() -> None:
    job_id = uuid.uuid4()
    task = MagicMock()
    task.name = "orchestrate_template_job"
    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = "cancelled"
    db = MagicMock()
    db.execute = AsyncMock(return_value=status_result)

    returned = await enqueue_orchestrator(task, job_id, db)

    assert returned == str(job_id)
    task.apply_async.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_enqueue_orchestrator_publish_failure_terminalizes_only_queued_job() -> None:
    job_id = uuid.uuid4()
    task = MagicMock()
    task.name = "orchestrate_template_job"
    events: list[str] = []
    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = "queued"
    recovery_result = MagicMock()
    recovery_result.rowcount = 1

    async def _execute(_stmt):
        events.append("status_read" if not events else "recovery_update")
        return status_result if len(events) == 1 else recovery_result

    def _publish(**_kwargs):
        events.append("broker_publish")
        raise RuntimeError("broker unavailable")

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    task.apply_async.side_effect = _publish

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await enqueue_orchestrator(task, job_id, db)

    assert events == ["status_read", "broker_publish", "recovery_update"]
    recovery_stmt = db.execute.await_args_list[-1].args[0]
    assert "jobs.status =" in str(recovery_stmt)
    assert "FOR UPDATE" not in str(recovery_stmt)
    assert "processing_failed" in set(recovery_stmt.compile().params.values())
    assert "dispatch_publish_failed" in set(recovery_stmt.compile().params.values())
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_orchestrator_publish_failure_preserves_newer_state() -> None:
    job_id = uuid.uuid4()
    task = MagicMock()
    task.name = "orchestrate_template_job"
    task.apply_async.side_effect = RuntimeError("ambiguous publish")
    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = "queued"
    recovery_result = MagicMock()
    recovery_result.rowcount = 0
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[status_result, recovery_result])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    with pytest.raises(RuntimeError, match="ambiguous publish"):
        await enqueue_orchestrator(task, job_id, db)

    # A worker claim or cancellation made the queued-only CAS lose. Recovery
    # commits no Job mutation and preserves the newer state.
    db.commit.assert_awaited_once()


def test_enqueue_orchestrator_sync_does_not_publish_cancelled_job() -> None:
    job_id = uuid.uuid4()
    task = MagicMock()
    task.name = "orchestrate_generative_job"
    result = MagicMock()
    result.scalar_one_or_none.return_value = "cancelled"
    session = MagicMock()
    session.execute.return_value = result
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False

    with patch("app.database.sync_session", return_value=context):
        returned = enqueue_orchestrator_sync(task, job_id)

    assert returned == str(job_id)
    task.apply_async.assert_not_called()


def test_enqueue_orchestrator_sync_publish_failure_recovers_after_broker_io() -> None:
    job_id = uuid.uuid4()
    task = MagicMock()
    task.name = "orchestrate_generative_job"
    events: list[str] = []

    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = "queued"
    status_session = MagicMock()
    status_session.execute.side_effect = lambda _stmt: events.append("status_read") or status_result
    status_context = MagicMock()
    status_context.__enter__.return_value = status_session
    status_context.__exit__.return_value = False

    recovery_result = MagicMock()
    recovery_result.rowcount = 1
    recovery_session = MagicMock()
    recovery_session.execute.side_effect = (
        lambda _stmt: events.append("recovery_update") or recovery_result
    )
    recovery_context = MagicMock()
    recovery_context.__enter__.return_value = recovery_session
    recovery_context.__exit__.return_value = False

    def _publish(**_kwargs):
        events.append("broker_publish")
        raise RuntimeError("broker unavailable")

    task.apply_async.side_effect = _publish
    with (
        patch(
            "app.database.sync_session",
            side_effect=[status_context, recovery_context],
        ),
        pytest.raises(RuntimeError, match="broker unavailable"),
    ):
        enqueue_orchestrator_sync(task, job_id)

    assert events == ["status_read", "broker_publish", "recovery_update"]
    recovery_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_enqueue_orchestrator_swallows_db_write_failure() -> None:
    """Task is already on the broker — a column-write failure must not raise.

    The reaper's inspect-by-args fallback still finds the task, so a
    failed celery_task_id write is recoverable and should not block the
    request.
    """
    job_id = uuid.uuid4()
    task = MagicMock()
    task.name = "orchestrate_template_job"
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db blip"))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    # No exception even though execute raised.
    returned = await enqueue_orchestrator(task, job_id, db)

    assert returned == str(job_id)
    task.apply_async.assert_called_once()
    db.rollback.assert_awaited_once()


def test_all_orchestrator_dispatches_use_helper() -> None:
    """Regression guard: every orchestrator dispatch must route through the helper.

    Scans every .py under app/ for `<orchestrator>.delay(` or
    `<orchestrator>.apply_async(`. Each match is OK if (a) it occurs in
    drive_import.py (the documented sync exception, which already sets
    task_id=job_id explicitly), or (b) it's inside app/services/job_dispatch.py
    itself, or (c) the call site is the helper invocation (i.e. the file
    that ALSO contains `enqueue_orchestrator(` on the same task name).

    Add new orchestrators to ORCHESTRATOR_TASK_NAMES — do not silently
    broaden this grep.
    """
    offenders: list[str] = []

    for path in sorted(API_ROOT.rglob("*.py")):
        # Skip tests, migrations, and the helper itself.
        if any(part in path.parts for part in ("migrations", "__pycache__")):
            continue
        if path.name == "job_dispatch.py":
            continue

        text = path.read_text(encoding="utf-8")

        for task_name in ORCHESTRATOR_TASK_NAMES:
            # Patterns we WILL flag:
            #   orchestrate_X.delay(...)
            #   orchestrate_X.apply_async(...)
            # Pattern we WON'T flag (helper handles it):
            #   enqueue_orchestrator(orchestrate_X, ...)
            for match in re.finditer(rf"\b{task_name}\.(delay|apply_async)\b", text):
                # drive_import.py predates the helper but DOES set task_id=job_id.
                # The helper docs document this as the one exception.
                if path.name == "drive_import.py":
                    # Only allow if the line includes task_id=
                    line_start = text.rfind("\n", 0, match.start()) + 1
                    line_end = text.find(";", match.end())
                    if line_end == -1:
                        line_end = len(text)
                    # Find the end of the call (next close-paren on its own line
                    # or balanced parens). Cheap heuristic: scan forward 200 chars.
                    snippet = text[line_start : match.end() + 200]
                    if "task_id=" in snippet:
                        continue
                rel = path.relative_to(API_ROOT.parent)
                offenders.append(f"{rel}:{match.start()} → {task_name}.{match.group(1)}")

    assert not offenders, (
        "These orchestrator dispatch sites bypass app/services/job_dispatch.enqueue_orchestrator "
        "and will not have celery_task_id persisted on the Job row:\n  " + "\n  ".join(offenders)
    )
