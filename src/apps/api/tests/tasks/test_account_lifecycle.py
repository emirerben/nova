from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from billiard.exceptions import SoftTimeLimitExceeded

from app.services.video_poster_cleanup import VideoPosterCleanupResult
from app.tasks.account_lifecycle import (
    cleanup_job_storage_paths,
    purge_job_storage,
    purge_user_storage,
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


def test_purge_user_storage_covers_every_job_output_and_legacy_prefix() -> None:
    user_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    raw_path = f"dev-user/{user_id}/generative/upload/source.mp4"
    prefix_calls: list[str] = []
    object_calls: list[str] = []

    with (
        patch(
            "app.storage.delete_prefix_best_effort",
            side_effect=lambda prefix: prefix_calls.append(prefix) or 1,
        ),
        patch(
            "app.storage.delete_object_best_effort",
            side_effect=lambda path: object_calls.append(path) or True,
        ),
    ):
        result = purge_user_storage.run(
            user_id,
            [job_id, "../foreign"],
            [raw_path],
        )

    assert prefix_calls == [
        f"users/{user_id}/",
        f"generative-jobs/{job_id}/",
        f"jobs/{job_id}/",
        f"music-jobs/{job_id}/",
        f"auto-music-jobs/{job_id}/",
        f"{user_id}/{job_id}/",
    ]
    assert object_calls == [raw_path]
    assert result == {
        "user_prefix_objects_deleted": 1,
        "job_prefix_objects_deleted": 5,
        "raw_paths_deleted": 1,
    }


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
        patch(
            "app.tasks.account_lifecycle.jobs_with_video_poster_cleanup_receipts",
            return_value=[],
        ) as select_poster_jobs,
        patch.object(purge_job_storage, "apply_async") as apply_async,
    ):
        result = sweep_job_storage_deletions.run(limit=10)

    assert result == {
        "dispatched": 1,
        "dispatch_failed": 0,
        "pruned": 2,
        "poster_receipts_scanned": 0,
        "poster_receipts_deleted": 0,
        "poster_receipts_pending": 0,
        "poster_receipt_failures": 0,
    }
    apply_async.assert_called_once_with(args=[outbox_id])
    select_poster_jobs.assert_called_once_with(session, limit=2)


def test_sweep_retries_pending_poster_receipt_on_next_beat() -> None:
    job_id = uuid.uuid4()
    session = MagicMock()
    empty_due = MagicMock()
    empty_due.scalars.return_value = []
    session.execute.side_effect = [
        empty_due,
        MagicMock(rowcount=0),
        empty_due,
        MagicMock(rowcount=0),
    ]
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False

    with (
        patch("app.tasks.account_lifecycle.sync_session", return_value=context),
        patch(
            "app.tasks.account_lifecycle.jobs_with_video_poster_cleanup_receipts",
            return_value=[job_id],
        ),
        patch(
            "app.tasks.account_lifecycle.reconcile_video_poster_cleanup_receipts",
            side_effect=[
                VideoPosterCleanupResult(
                    receipts_seen=1,
                    retained=1,
                    failures=1,
                ),
                VideoPosterCleanupResult(receipts_seen=1, deleted=1),
            ],
        ) as reconcile,
    ):
        first = sweep_job_storage_deletions.run(limit=10)
        second = sweep_job_storage_deletions.run(limit=10)

    assert first["poster_receipts_pending"] == 1
    assert first["poster_receipt_failures"] == 1
    assert second["poster_receipts_deleted"] == 1
    assert second["poster_receipts_pending"] == 0
    assert reconcile.call_args_list[0].args == (job_id,)
    assert reconcile.call_args_list[1].args == (job_id,)


def test_sweep_continues_after_one_poster_reconcile_raises() -> None:
    failed_job_id = uuid.uuid4()
    succeeding_job_id = uuid.uuid4()
    session = MagicMock()
    empty_due = MagicMock()
    empty_due.scalars.return_value = []
    session.execute.side_effect = [empty_due, MagicMock(rowcount=0)]
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False

    with (
        patch("app.tasks.account_lifecycle.sync_session", return_value=context),
        patch(
            "app.tasks.account_lifecycle.jobs_with_video_poster_cleanup_receipts",
            return_value=[failed_job_id, succeeding_job_id],
        ),
        patch(
            "app.tasks.account_lifecycle.reconcile_video_poster_cleanup_receipts",
            side_effect=[
                RuntimeError("first job storage unavailable"),
                VideoPosterCleanupResult(receipts_seen=1, deleted=1),
            ],
        ) as reconcile,
    ):
        result = sweep_job_storage_deletions.run(limit=10)

    assert result == {
        "dispatched": 0,
        "dispatch_failed": 0,
        "pruned": 0,
        "poster_receipts_scanned": 1,
        "poster_receipts_deleted": 1,
        "poster_receipts_pending": 0,
        "poster_receipt_failures": 1,
    }
    assert [call.args for call in reconcile.call_args_list] == [
        (failed_job_id,),
        (succeeding_job_id,),
    ]


def test_sweep_propagates_soft_time_limit_instead_of_starting_another_job() -> None:
    first_job_id = uuid.uuid4()
    untouched_job_id = uuid.uuid4()
    session = MagicMock()
    empty_due = MagicMock()
    empty_due.scalars.return_value = []
    session.execute.side_effect = [empty_due, MagicMock(rowcount=0)]
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False

    with (
        patch("app.tasks.account_lifecycle.sync_session", return_value=context),
        patch(
            "app.tasks.account_lifecycle.jobs_with_video_poster_cleanup_receipts",
            return_value=[first_job_id, untouched_job_id],
        ),
        patch(
            "app.tasks.account_lifecycle.reconcile_video_poster_cleanup_receipts",
            side_effect=SoftTimeLimitExceeded(),
        ) as reconcile,
        pytest.raises(SoftTimeLimitExceeded),
    ):
        sweep_job_storage_deletions.run(limit=10)

    assert [call.args for call in reconcile.call_args_list] == [(first_job_id,)]
