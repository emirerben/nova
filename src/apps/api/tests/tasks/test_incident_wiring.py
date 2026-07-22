"""Wiring pins for the 2026-07-21 OOM incident fixes.

The units (source_guard, job_heartbeat, _compute_retrying) are tested in
isolation elsewhere — these tests pin the WIRING, where every failure mode is
silent: dropping `job_heartbeat` from the orchestrator's `with` statement
leaves the beacon NULL and `retrying` false forever; dropping the
`downscale_oversized_sources` call from `_ingest_clips` regresses the OOM fix
with all unit guards green.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import pytest


def test_orchestrator_enters_job_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.tasks.generative_build as gb

    entered: list[str] = []

    @contextlib.contextmanager
    def _fake_heartbeat(job_id):
        entered.append(str(job_id))
        yield

    monkeypatch.setattr(gb, "job_heartbeat", _fake_heartbeat)
    monkeypatch.setattr(gb, "mark_started", lambda job_id: None)
    monkeypatch.setattr(gb, "mark_finished", lambda job_id: None)
    monkeypatch.setattr(gb, "_run_generative_job", lambda job_id: None)

    import app.services.pipeline_trace as pt

    monkeypatch.setattr(pt, "pipeline_trace_for", lambda job_id: contextlib.nullcontext())

    gb.orchestrate_generative_job("11111111-2222-3333-4444-555555555555")

    assert entered == ["11111111-2222-3333-4444-555555555555"]


def test_ingest_clips_runs_downscale_guard_before_maps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must run inside _ingest_clips, mutating the path list BEFORE
    the clip_id maps are built — so every downstream consumer (Gemini upload,
    variant reframes, footage sum) sees the bounded intermediates."""
    import app.pipeline.source_guard as sg
    import app.tasks.generative_build as gb
    import app.tasks.template_orchestrate as to

    monkeypatch.setattr(to, "_download_clips_parallel", lambda paths, tmpdir: ["/tmp/a.mp4"])
    fake_probe = MagicMock()
    monkeypatch.setattr(to, "_probe_clips", lambda paths: {"/tmp/a.mp4": fake_probe})

    def _fake_guard(local_paths, probe_map, tmpdir, *, job_id=None):
        # Simulate a conversion: repoint the list in place, like prod does.
        probe_map["/tmp/guard_a.mp4"] = probe_map.pop(local_paths[0])
        local_paths[0] = "/tmp/guard_a.mp4"
        return 1

    monkeypatch.setattr(sg, "downscale_oversized_sources", _fake_guard)

    ingest = gb._ingest_clips(["gs/a.mp4"], "/tmp", job_id="j", skip_analysis=True)

    # The maps must reflect the guard's mutation — proof it ran before them.
    assert ingest["clip_id_to_local"] == {"clip_0": "/tmp/guard_a.mp4"}
    assert "/tmp/guard_a.mp4" in ingest["probe_map"]


def test_task_prerun_signal_triggers_tmp_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting the @task_prerun.connect receiver in worker.py must fail a test —
    the sweep is the redelivered attempt's only tmpfs reclaim, and a dropped
    signal hookup is silent everywhere else."""
    from celery.signals import task_prerun

    import app.pipeline.tmp_sweep as ts
    import app.worker  # noqa: F401 — importing connects the receiver

    calls: list[int] = []
    monkeypatch.setattr(ts, "sweep_stale_nova_tmpdirs", lambda: calls.append(1))

    task_prerun.send(sender=None, task_id="t", task=None, args=(), kwargs={})

    assert calls, "task_prerun must trigger sweep_stale_nova_tmpdirs (worker.py receiver)"


def test_heavy_render_paths_call_downscale_guard() -> None:
    """Source pin: the timeline re-render and bed-level reburn both decode
    DURABLE ORIGINALS — an un-guarded path reproduces the 2026-07-21 OOM
    verbatim, and dropping the call is invisible to their behavior tests."""
    import inspect

    import app.tasks.generative_build as gb

    for fn in (gb._prepare_timeline_assembly, gb._run_reburn_narrated_bed_level):
        assert "downscale_oversized_sources(" in inspect.getsource(fn), (
            f"{fn.__name__} no longer runs the heavy-source guard — a 4K "
            "re-render through this path reproduces prod job e8173a25's OOM"
        )


def test_beat_heartbeat_issues_readfree_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the REAL beat_heartbeat statement (every other test mocks the
    whole function): it must be an UPDATE of worker_heartbeat_at to the DB
    clock, never a read-modify-write of JSONB state."""
    from sqlalchemy.dialects import postgresql

    from app.services import job_phases

    captured: list[object] = []

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, stmt):
            captured.append(stmt)

        def commit(self):
            pass

    monkeypatch.setattr(job_phases, "sync_session", lambda: _FakeSession())

    job_phases.beat_heartbeat("11111111-2222-3333-4444-555555555555")

    assert len(captured) == 1
    sql = str(captured[0].compile(dialect=postgresql.dialect()))
    assert "worker_heartbeat_at" in sql
    assert "now()" in sql  # DB clock, not a worker-VM datetime parameter
    assert "assembly_plan" not in sql  # read-free contract


def test_tmp_sweep_removes_only_stale_nova_dirs(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import time

    import app.pipeline.tmp_sweep as ts

    monkeypatch.setattr(ts.tempfile, "gettempdir", lambda: str(tmp_path))

    old = time.time() - 4000
    stale_nova = tmp_path / "nova_generative_dead"
    stale_nova.mkdir()
    os.utime(stale_nova, (old, old))
    fresh_nova = tmp_path / "nova_generative_live"
    fresh_nova.mkdir()
    stale_other = tmp_path / "pytest-of-someone"
    stale_other.mkdir()
    os.utime(stale_other, (old, old))
    stale_nova_file = tmp_path / "nova_notes.txt"
    stale_nova_file.write_bytes(b"x")
    os.utime(stale_nova_file, (old, old))

    removed = ts.sweep_stale_nova_tmpdirs()

    assert removed == 1
    assert not stale_nova.exists()  # old + nova + dir → swept
    assert fresh_nova.exists()  # fresh → kept (sibling-worker safety)
    assert stale_other.exists()  # non-nova → never touched
    assert stale_nova_file.exists()  # files → never touched


def test_tmp_sweep_cutoff_stays_inside_redelivery_window() -> None:
    """The cutoff must exceed every render task's hard time_limit (a live
    sibling task cannot own a dir older than its own limit) and stay at or
    under the broker visibility_timeout (so a dead attempt's dirs are
    sweepable by the time its redelivery arrives)."""
    from app.pipeline.tmp_sweep import _MAX_AGE_S
    from app.worker import celery_app

    visibility_timeout = int(celery_app.conf.broker_transport_options["visibility_timeout"])
    render_hard_limit = 1800  # pinned by tests/tasks/test_task_time_limits.py
    assert render_hard_limit < _MAX_AGE_S <= visibility_timeout
