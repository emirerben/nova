from __future__ import annotations

import uuid
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import sync_session
from app.models import Job, JobClip, User
from app.tasks._finalization_commit import (
    FinalizationCommitState,
    confirm_job_clip_finalization,
    confirm_job_plan_finalization,
)


class _Session:
    def __init__(self, rows: dict[type, object]) -> None:
        self.rows = rows
        self.get_calls: list[type] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, model, _row_id, **kwargs):
        assert kwargs == {"populate_existing": True, "with_for_update": True}
        self.get_calls.append(model)
        return self.rows.get(model)


@pytest.mark.parametrize(
    ("status", "plan", "expected"),
    [
        (
            "template_ready",
            {
                "output_path": "jobs/j/task-runs/a/output.mp4",
                "poster_path": "jobs/j/task-runs/a/output.poster.jpg",
            },
            FinalizationCommitState.CONFIRMED,
        ),
        (
            "processing",
            {
                "output_path": "jobs/j/task-runs/a/output.mp4",
                "poster_path": "jobs/j/old.poster.jpg",
            },
            FinalizationCommitState.UNKNOWN,
        ),
        (
            "processing",
            {
                "output_path": "jobs/j/task-runs/older/output.mp4",
                "poster_path": "jobs/j/older.poster.jpg",
            },
            FinalizationCommitState.NOT_COMMITTED,
        ),
        (
            "template_ready",
            {
                "output_path": "jobs/j/task-runs/newer/output.mp4",
                "poster_path": "jobs/j/newer.poster.jpg",
            },
            FinalizationCommitState.UNKNOWN,
        ),
    ],
)
def test_job_plan_commit_proof_distinguishes_exact_partial_and_absent_attempt(
    status: str,
    plan: dict,
    expected: FinalizationCommitState,
) -> None:
    job_id = uuid.uuid4()
    job = SimpleNamespace(id=job_id, status=status, assembly_plan=plan)

    result = confirm_job_plan_finalization(
        lambda: _Session({Job: job}),
        job_id=job_id,
        expected_status="template_ready",
        expected_plan_fields={
            "output_path": "jobs/j/task-runs/a/output.mp4",
            "poster_path": "jobs/j/task-runs/a/output.poster.jpg",
        },
        attempt_references=(
            "jobs/j/task-runs/a/output.mp4",
            "jobs/j/task-runs/a/output.poster.jpg",
        ),
    )

    assert result is expected


def test_job_plan_commit_proof_fails_closed_when_fresh_read_fails() -> None:
    def fail_session():
        raise RuntimeError("database still unavailable")

    assert (
        confirm_job_plan_finalization(
            fail_session,
            job_id=uuid.uuid4(),
            expected_status="music_ready",
            expected_plan_fields={"output_path": "attempt.mp4"},
            attempt_references=("attempt.mp4",),
        )
        is FinalizationCommitState.UNKNOWN
    )


@pytest.mark.parametrize(
    ("clip_fields", "plan", "expected"),
    [
        (
            {
                "render_status": "ready",
                "video_path": "https://cdn.test/attempt.mp4",
                "thumbnail_path": "attempt.jpg",
            },
            {},
            FinalizationCommitState.CONFIRMED,
        ),
        (
            {
                "render_status": "rendering",
                "video_path": "https://cdn.test/attempt.mp4",
                "thumbnail_path": "old.jpg",
            },
            {},
            FinalizationCommitState.UNKNOWN,
        ),
        (
            {
                "render_status": "rendering",
                "video_path": "https://cdn.test/old.mp4",
                "thumbnail_path": "old.jpg",
            },
            {"cleanup": [{"replacement_path": "attempt.jpg"}]},
            FinalizationCommitState.UNKNOWN,
        ),
        (
            {
                "render_status": "rendering",
                "video_path": "https://cdn.test/old.mp4",
                "thumbnail_path": "old.jpg",
            },
            {},
            FinalizationCommitState.NOT_COMMITTED,
        ),
    ],
)
def test_clip_commit_proof_never_calls_partial_reference_safe_to_delete(
    clip_fields: dict,
    plan: dict,
    expected: FinalizationCommitState,
) -> None:
    job_id = uuid.uuid4()
    clip_id = uuid.uuid4()
    job = SimpleNamespace(id=job_id, assembly_plan=plan)
    clip = SimpleNamespace(id=clip_id, job_id=job_id, **clip_fields)

    result = confirm_job_clip_finalization(
        lambda: _Session({Job: job, JobClip: clip}),
        job_id=job_id,
        clip_id=clip_id,
        expected_clip_fields={
            "render_status": "ready",
            "video_path": "https://cdn.test/attempt.mp4",
            "thumbnail_path": "attempt.jpg",
        },
        attempt_references=("https://cdn.test/attempt.mp4", "attempt.jpg"),
    )

    assert result is expected


def test_clip_commit_proof_retains_attempt_referenced_by_reassigned_clip() -> None:
    job_id = uuid.uuid4()
    clip_id = uuid.uuid4()
    job = SimpleNamespace(id=job_id, assembly_plan={})
    clip = SimpleNamespace(
        id=clip_id,
        job_id=uuid.uuid4(),
        render_status="rendering",
        video_path="https://cdn.test/attempt.mp4",
        thumbnail_path="old.jpg",
    )

    result = confirm_job_clip_finalization(
        lambda: _Session({Job: job, JobClip: clip}),
        job_id=job_id,
        clip_id=clip_id,
        expected_clip_fields={
            "render_status": "ready",
            "video_path": "https://cdn.test/attempt.mp4",
            "thumbnail_path": "attempt.jpg",
        },
        attempt_references=("https://cdn.test/attempt.mp4", "attempt.jpg"),
    )

    assert result is FinalizationCommitState.UNKNOWN


def test_clip_commit_proof_locks_job_before_clip() -> None:
    job_id = uuid.uuid4()
    clip_id = uuid.uuid4()
    session = _Session(
        {
            Job: SimpleNamespace(id=job_id, assembly_plan={}),
            JobClip: SimpleNamespace(
                id=clip_id,
                job_id=job_id,
                render_status="rendering",
                video_path=None,
                thumbnail_path=None,
            ),
        }
    )

    confirm_job_clip_finalization(
        lambda: session,
        job_id=job_id,
        clip_id=clip_id,
        expected_clip_fields={
            "render_status": "ready",
            "video_path": "attempt.mp4",
            "thumbnail_path": "attempt.jpg",
        },
        attempt_references=("attempt.mp4", "attempt.jpg"),
    )

    assert session.get_calls == [Job, JobClip]


def test_job_plan_commit_proof_waits_for_in_flight_finalizer() -> None:
    """The proof must not classify a pre-COMMIT snapshot as safe to delete."""
    db_name = make_url(settings.database_url).database or ""
    if not db_name.endswith("_test"):
        pytest.skip(f"refusing to write to non-test database {db_name!r}")
    try:
        with sync_session() as probe:
            probe.execute(text("select 1"))
    except OperationalError:
        pytest.skip("nova_test Postgres not reachable")

    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    output_path = f"jobs/{job_id}/task-runs/a/output.mp4"
    poster_path = f"jobs/{job_id}/task-runs/a/output.poster.jpg"
    writer_ready = Event()
    release_writer = Event()
    verifier_done = Event()
    writer_errors: list[BaseException] = []
    verification_results: list[FinalizationCommitState] = []

    with sync_session() as db:
        db.add(User(id=user_id, email=f"finalization-proof-{user_id}@test.local"))
        db.add(
            Job(
                id=job_id,
                user_id=user_id,
                status="processing",
                job_type="template",
                raw_storage_path=f"jobs/{job_id}/raw.mp4",
                assembly_plan={},
            )
        )
        db.commit()

    def commit_finalization() -> None:
        try:
            with sync_session() as db:
                job = db.get(Job, job_id, with_for_update=True)
                assert job is not None
                job.status = "template_ready"
                job.assembly_plan = {
                    "output_path": output_path,
                    "poster_path": poster_path,
                }
                db.flush()
                writer_ready.set()
                if not release_writer.wait(timeout=5):
                    raise TimeoutError("test did not release finalizer transaction")
                db.commit()
        except BaseException as exc:  # noqa: BLE001 - relay thread failures
            writer_errors.append(exc)
            writer_ready.set()

    def verify_finalization() -> None:
        verification_results.append(
            confirm_job_plan_finalization(
                sync_session,
                job_id=job_id,
                expected_status="template_ready",
                expected_plan_fields={
                    "output_path": output_path,
                    "poster_path": poster_path,
                },
                attempt_references=(output_path, poster_path),
            )
        )
        verifier_done.set()

    writer = Thread(target=commit_finalization, daemon=True)
    verifier = Thread(target=verify_finalization, daemon=True)
    try:
        writer.start()
        assert writer_ready.wait(timeout=5), "writer never acquired the Job lock"
        assert writer_errors == []
        verifier.start()
        assert not verifier_done.wait(timeout=0.25), (
            "verification returned before the in-flight COMMIT resolved"
        )
    finally:
        release_writer.set()
        writer.join(timeout=5)
        verifier.join(timeout=5)
        with sync_session() as cleanup:
            cleanup.execute(text("delete from jobs where id = :job_id"), {"job_id": job_id})
            cleanup.execute(text("delete from users where id = :user_id"), {"user_id": user_id})
            cleanup.commit()

    assert not writer.is_alive()
    assert not verifier.is_alive()
    assert writer_errors == []
    assert verification_results == [FinalizationCommitState.CONFIRMED]
