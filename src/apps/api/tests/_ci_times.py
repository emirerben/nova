"""Per-test timing rows for CI. Inert unless NOVA_CI_TIMES_DIR is set.

Each pytest process appends JSON lines to `<dir>/<worker>.jsonl`:

- one `startup` row: seconds from plugin load to the end of collection, how
  many tests that process collected, and the name of the database it is on.
  More cores do not shrink this part;
- one `test` row per test: outcome, wall seconds of setup, call and teardown,
  CPU seconds of this process plus the children it waited for (ffmpeg), and
  the timeout that applies to it (its own marker, else the run's default).
  Wall time far above CPU time means the test was waiting, not computing.

Rows are written as the tests finish, so a crashed worker keeps what it ran.
`scripts/ci/api-tests.py` sets the directory; `scripts/ci/api-times.py` reads
the rows. Guard: tests/test_ci_times_recorder.py.
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

ENV = "NOVA_CI_TIMES_DIR"
_LOADED = time.perf_counter()


def database_name(modules: Mapping[str, object], environ: Mapping[str, str]) -> str:
    """The name of the database this process's app is on.

    The app reads its URL once, when `app.config` is imported, so a loaded app
    is asked rather than the environment: if something imported it before the
    worker was moved to its own database, the environment says one thing and
    every query goes somewhere else. Name only; the URL carries a password.
    """
    settings = getattr(modules.get("app.config"), "settings", None)
    url = getattr(settings, "database_url", None) or environ.get("DATABASE_URL", "")
    return str(url).rsplit("/", 1)[-1].split("?", 1)[0]


def _cpu_seconds() -> float:
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


def _outcome(report) -> str | None:
    """The outcome one phase report settles, or None if it settles nothing."""
    if report.failed:
        return "failed" if report.when == "call" else "error"
    if report.skipped:
        return "xfailed" if hasattr(report, "wasxfail") else "skipped"
    if report.when == "call":
        return "xpassed" if hasattr(report, "wasxfail") else "passed"
    return None


def _timeout(item) -> float | None:
    """Seconds pytest-timeout allows this test: its marker, else the run's setting."""
    marker = item.get_closest_marker("timeout")
    value = None
    if marker is not None:
        value = marker.args[0] if marker.args else marker.kwargs.get("timeout")
    if value is None:
        value = item.config.getoption("timeout", None)
    if value is None:
        try:
            value = item.config.getini("timeout")
        except ValueError:  # pytest-timeout is not installed
            value = None
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def _reason(report) -> str:
    if hasattr(report, "wasxfail"):
        return str(report.wasxfail)
    if isinstance(report.longrepr, tuple):
        return str(report.longrepr[2])
    return ""


class Recorder:
    def __init__(self, directory: Path, worker: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.worker = worker
        # Line-buffered: a worker that dies keeps every finished test.
        self.handle = (directory / f"{worker}.jsonl").open("a", buffering=1)
        self.pending: dict[str, dict] = {}

    def write(self, row: dict) -> None:
        self.handle.write(json.dumps(row, sort_keys=True) + "\n")

    def pytest_collection_finish(self, session) -> None:
        self.write(
            {
                "kind": "startup",
                "worker": self.worker,
                # With one database per xdist worker, scripts/ci/api-tests.py fails the
                # shard unless this is the worker's own.
                "database": database_name(sys.modules, os.environ),
                "seconds": round(time.perf_counter() - _LOADED, 3),
                "items": len(session.items),
            }
        )

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        row = {
            "kind": "test",
            "worker": self.worker,
            "nodeid": item.nodeid,
            "file": item.nodeid.split("::", 1)[0],
            "outcome": "passed",
            "reason": "",
            "setup": 0,
            "call": 0,
            "teardown": 0,
            "timeout": _timeout(item),
        }
        self.pending[item.nodeid] = row
        cpu, wall = _cpu_seconds(), time.perf_counter()
        try:
            return (yield)
        finally:
            row["cpu"] = round(_cpu_seconds() - cpu, 3)
            row["wall"] = round(time.perf_counter() - wall, 3)
            self.write(self.pending.pop(item.nodeid))

    def pytest_runtest_logreport(self, report) -> None:
        row = self.pending.get(report.nodeid)
        if row is None:
            return
        row[report.when] = round(report.duration, 3)
        outcome = _outcome(report)
        # The first failing phase wins: a failed call is not undone by teardown.
        if outcome and row["outcome"] in ("passed", "xpassed"):
            row["outcome"] = outcome
            row["reason"] = _reason(report) if outcome != "passed" else ""

    def pytest_unconfigure(self, config) -> None:
        self.handle.close()


def pytest_configure(config) -> None:
    directory = os.environ.get(ENV)
    if not directory:
        return
    worker_input = getattr(config, "workerinput", None)
    if worker_input is None and (
        config.getoption("numprocesses", None) or config.getoption("dist", "no") != "no"
    ):
        # The xdist controller sees every worker's reports; the workers write them.
        return
    worker = worker_input["workerid"] if worker_input else "main"
    # This process runs tests. Several of them start their own pytest; without
    # this, each child would add its tests (some meant to fail) to these rows.
    del os.environ[ENV]
    config.pluginmanager.register(Recorder(Path(directory).resolve(), worker), "nova-ci-times")
