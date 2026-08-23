from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.tasks import edit_training_artifacts as task


@contextmanager
def _session_returning(jobs):  # noqa: ANN001
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = jobs
    yield db


def _job(*, created_at: datetime, ready: bool = True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        created_at=created_at,
        assembly_plan={
            "variants": [
                {
                    "variant_id": "v1",
                    "render_generation_id": "generation-1",
                    "render_status": "ready" if ready else "rendering",
                    "video_path": "generative-jobs/final.mp4",
                }
            ]
        },
    )


def test_internal_backfill_pages_until_every_historic_job_is_scanned(monkeypatch) -> None:
    now = datetime.now(UTC)
    jobs = [_job(created_at=now), _job(created_at=now - timedelta(seconds=1))]
    monkeypatch.setattr(task, "sync_session", lambda: _session_returning(jobs))
    capture = MagicMock()
    continuation = MagicMock()
    monkeypatch.setattr(task.capture_edit_training_artifact, "delay", capture)
    monkeypatch.setattr(task.backfill_edit_training_artifacts, "delay", continuation)

    creator_id = uuid.uuid4()
    result = task.backfill_edit_training_artifacts.run(str(creator_id), 2)

    assert result["jobs_scanned"] == 2
    assert result["continuation_queued"] is True
    assert capture.call_count == 2
    continuation.assert_called_once_with(
        str(creator_id),
        2,
        jobs[-1].created_at.isoformat(),
        str(jobs[-1].id),
    )


def test_internal_backfill_stops_only_after_short_terminal_page(monkeypatch) -> None:
    jobs = [_job(created_at=datetime.now(UTC), ready=False)]
    monkeypatch.setattr(task, "sync_session", lambda: _session_returning(jobs))
    capture = MagicMock()
    continuation = MagicMock()
    monkeypatch.setattr(task.capture_edit_training_artifact, "delay", capture)
    monkeypatch.setattr(task.backfill_edit_training_artifacts, "delay", continuation)

    result = task.backfill_edit_training_artifacts.run(str(uuid.uuid4()), 300)

    assert result == {
        "status": "queued",
        "count": 0,
        "jobs_scanned": 1,
        "continuation_queued": False,
    }
    capture.assert_not_called()
    continuation.assert_not_called()
