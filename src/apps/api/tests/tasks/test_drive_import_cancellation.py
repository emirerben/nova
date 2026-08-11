from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.tasks.drive_import import _claim_import_job, import_from_drive


def _job(*, status: str, task_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        celery_task_id=task_id,
        started_at=None,
        worker_heartbeat_at=None,
        probe_metadata={"drive_file_size_bytes": 123},
        finished_at=None,
        failure_reason=None,
        error_detail=None,
    )


def _context(session: MagicMock) -> MagicMock:
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False
    return context


def test_claim_import_persists_started_fence_before_io() -> None:
    task_id = str(uuid.uuid4())
    job = _job(status="importing", task_id=task_id)
    session = MagicMock()
    session.get.return_value = job

    assert _claim_import_job(session, str(job.id), task_id) == 123
    assert job.started_at is not None
    assert job.worker_heartbeat_at is not None
    session.commit.assert_called_once()


def test_cancelled_or_stale_import_delivery_fails_closed() -> None:
    task_id = str(uuid.uuid4())
    for job in (
        _job(status="cancelled", task_id=task_id),
        _job(status="importing", task_id=str(uuid.uuid4())),
    ):
        session = MagicMock()
        session.get.return_value = job
        assert _claim_import_job(session, str(job.id), task_id) is None
        session.commit.assert_not_called()


def test_import_task_checks_cancelled_state_before_drive_io() -> None:
    task_id = str(uuid.uuid4())
    job = _job(status="cancelled", task_id=task_id)
    session = MagicMock()
    session.get.return_value = job
    redis = MagicMock()

    import_from_drive.push_request(id=task_id, retries=0)
    try:
        with (
            patch("app.tasks.drive_import._sync_session", return_value=_context(session)),
            patch("app.tasks.drive_import._decrypt_token") as decrypt,
            patch("app.tasks.drive_import._stream_download") as download,
            patch("app.tasks.drive_import._upload_to_gcs") as upload,
            patch("app.tasks.drive_import._get_redis", return_value=redis),
        ):
            import_from_drive.run(
                str(job.id),
                "drive-file-id",
                "encrypted",
                f"user/{job.id}/raw.mp4",
            )
    finally:
        import_from_drive.pop_request()

    decrypt.assert_not_called()
    download.assert_not_called()
    upload.assert_not_called()


def test_stale_failure_does_not_delete_raw_input_or_overwrite_handoff() -> None:
    task_id = str(uuid.uuid4())
    job_id = uuid.uuid4()
    claimed = _job(status="importing", task_id=task_id)
    claimed.id = job_id
    handed_off = _job(status="queued", task_id=str(job_id))
    handed_off.id = job_id
    claim_session = MagicMock()
    claim_session.get.return_value = claimed
    failure_session = MagicMock()
    failure_session.get.return_value = handed_off
    redis = MagicMock()

    import_from_drive.push_request(id=task_id, retries=0)
    try:
        with (
            patch(
                "app.tasks.drive_import._sync_session",
                side_effect=[_context(claim_session), _context(failure_session)],
            ),
            patch("app.tasks.drive_import._decrypt_token", return_value="token"),
            patch("app.tasks.drive_import._stream_download", side_effect=ValueError("download")),
            patch("app.tasks.drive_import._cleanup_gcs_blob") as cleanup,
            patch("app.tasks.drive_import._get_redis", return_value=redis),
        ):
            import_from_drive.run(
                str(job_id),
                "drive-file-id",
                "encrypted",
                f"user/{job_id}/raw.mp4",
            )
    finally:
        import_from_drive.pop_request()

    assert handed_off.status == "queued"
    assert handed_off.error_detail is None
    failure_session.commit.assert_not_called()
    cleanup.assert_not_called()
