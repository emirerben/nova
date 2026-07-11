"""Shared task-test fixtures.

`FakeJob` / `patch_job_session` were duplicated (and had diverged) between
test_generative_build.py and test_caption_reapply.py — single-sourced here,
parameterized to cover both variants. Plain importables (not pytest fixtures)
so call sites keep their explicit wiring.
"""

from __future__ import annotations

import uuid


class FakeJob:
    """Minimal stand-in for the ORM Job row the worker helpers read/write."""

    def __init__(
        self,
        assembly_plan=None,
        all_candidates=None,
        status: str = "rendering",
        job_id: str | uuid.UUID | None = None,
    ):
        if job_id is not None:
            self.id = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(job_id)
        self.assembly_plan = assembly_plan
        self.all_candidates = all_candidates or {}
        self.status = status


def patch_job_session(monkeypatch, job, *, noop_flag_modified: bool = False) -> None:
    """Make gb._sync_session() yield a session whose .get() returns `job`.

    `noop_flag_modified=True` also no-ops sqlalchemy's flag_modified — needed by
    code paths that mark the JSONB column dirty on the fake (non-ORM) job.
    """
    import app.tasks.generative_build as gb

    class _Sess:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, model, pk, **kw):
            return job

        def commit(self):
            pass

    monkeypatch.setattr(gb, "_sync_session", lambda: _Sess())
    if noop_flag_modified:
        monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *a, **k: None)
