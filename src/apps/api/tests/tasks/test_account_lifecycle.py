from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.tasks.account_lifecycle import (
    cleanup_job_storage_paths,
    purge_job_storage,
    sweep_job_storage_deletions,
)


def test_cleanup_job_storage_paths_is_idempotent_and_returns_only_failures() -> None:
    calls: list[str] = []

    def delete(path: str) -> bool:
        calls.append(path)
        return path != "jobs/job-1/transient.mp4"

    with patch("app.storage.delete_object_best_effort", side_effect=delete):
        deleted, failed = cleanup_job_storage_paths(
            [
                "jobs/job-1/output.mp4",
                "jobs/job-1/transient.mp4",
                "jobs/job-1/output.mp4",
            ]
        )

    assert deleted == 1
    assert failed == ["jobs/job-1/transient.mp4"]
    assert calls == ["jobs/job-1/output.mp4", "jobs/job-1/transient.mp4"]


def _session_context(deletion: SimpleNamespace) -> MagicMock:
    session = MagicMock()
    session.execute.return_value.scalar_one_or_none.return_value = deletion
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False
    return context


def _deletion(paths: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        id="outbox-1",
        status="pending",
        object_paths=paths,
        attempts=0,
        next_attempt_at=None,
        lease_until=None,
        last_error=None,
        completed_at=None,
    )


def test_purge_job_storage_marks_manifest_complete_for_already_deleted_objects() -> None:
    deletion = _deletion(["jobs/job-1/output.mp4"])
    session = _session_context(deletion)
    with (
        patch("app.tasks.account_lifecycle.sync_session", return_value=session),
        patch("app.tasks.account_lifecycle.cleanup_job_storage_paths", return_value=(1, [])),
    ):
        result = purge_job_storage.run("outbox-1")

    assert result == {"status": "completed", "deleted": 1, "failed": 0}
    assert deletion.status == "completed"
    assert deletion.object_paths == []
    assert deletion.completed_at is not None


def test_purge_job_storage_persists_only_failed_objects_for_durable_retry() -> None:
    deletion = _deletion(["jobs/job-1/output.mp4", "jobs/job-1/transient.mp4"])
    session = _session_context(deletion)
    with (
        patch("app.tasks.account_lifecycle.sync_session", return_value=session),
        patch(
            "app.tasks.account_lifecycle.cleanup_job_storage_paths",
            return_value=(1, ["jobs/job-1/transient.mp4"]),
        ),
    ):
        result = purge_job_storage.run("outbox-1")

    assert result == {"status": "pending", "deleted": 1, "failed": 1}
    assert deletion.status == "pending"
    assert deletion.object_paths == ["jobs/job-1/transient.mp4"]
    assert deletion.next_attempt_at is not None
    assert deletion.last_error == "1 storage objects could not be deleted"


def test_sweep_job_storage_deletions_dispatches_due_rows_and_prunes_completed() -> None:
    outbox_id = "outbox-1"
    session = MagicMock()
    due_result = MagicMock()
    due_result.scalars.return_value = [outbox_id]
    prune_result = MagicMock(rowcount=2)
    session.execute.side_effect = [due_result, prune_result]
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False

    with (
        patch("app.tasks.account_lifecycle.sync_session", return_value=context),
        patch.object(purge_job_storage, "apply_async") as apply_async,
    ):
        result = sweep_job_storage_deletions.run(limit=10)

    assert result == {"dispatched": 1, "dispatch_failed": 0, "pruned": 2}
    apply_async.assert_called_once_with(args=[outbox_id])
