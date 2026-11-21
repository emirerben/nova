"""Ways the CI timing recorder could mislead, written before the plugin.

`tests/_ci_times.py` writes one row per test so CI changes are aimed with
measurements. Each test below names one way it could report a wrong number or
change the run it is measuring:

- under xdist the controller also sees every report, so each test is written
  twice and every total doubles;
- a test that breaks in setup has no call phase and is dropped, or is filed as
  an ordinary failure;
- a skip loses its reason, or an expected failure (xfail) is counted as a skip
  or a pass, so a silent skip cannot be told from a known gap;
- CPU spent in a child process (ffmpeg) is missed, so a CPU-bound test looks
  like it is waiting;
- a parametrised id containing "::" is attributed to the wrong file;
- the time before the first test (imports and collection) is not recorded per
  process, which is the part more cores do not shrink;
- a test that starts its own pytest (several in this suite do) passes the
  switch on to it, so the child's tests, including ones meant to fail, are
  added to the suite's rows;
- the recorder changes pytest's exit code, or writes files when it is not
  asked to.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]

SAMPLE = """
import subprocess
import sys
import time

import pytest


def test_pass():
    time.sleep(0.05)


def test_fail():
    assert False


def test_skip():
    pytest.skip("no ffmpeg here")


@pytest.mark.xfail(strict=True)
def test_known_gap():
    assert False


@pytest.fixture
def broken():
    raise RuntimeError("setup broke")


def test_setup_error(broken):
    pass


def test_child_cpu():
    subprocess.run(
        [sys.executable, "-c", "sum(i * i for i in range(4_000_000))"], check=True
    )


@pytest.mark.parametrize("value", ["a::b", "c"])
def test_param(value):
    pass


def test_runs_its_own_pytest(tmp_path):
    (tmp_path / "test_inner.py").write_text("def test_inner_fails():\\n    assert False\\n")
    inner = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests._ci_times", "-p", "no:cacheprovider",
         "-q", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert inner.returncode == 1, inner.stdout + inner.stderr
"""

NODES = {
    "checks/test_sample.py::test_pass": "passed",
    "checks/test_sample.py::test_fail": "failed",
    "checks/test_sample.py::test_skip": "skipped",
    "checks/test_sample.py::test_known_gap": "xfailed",
    "checks/test_sample.py::test_setup_error": "error",
    "checks/test_sample.py::test_child_cpu": "passed",
    "checks/test_sample.py::test_param[a::b]": "passed",
    "checks/test_sample.py::test_param[c]": "passed",
    "checks/test_sample.py::test_runs_its_own_pytest": "passed",
}


def run_sample(root: Path, *args: str, record: bool = True):
    project = root / "project"
    (project / "checks").mkdir(parents=True)
    (project / "checks/test_sample.py").write_text(SAMPLE)
    out = root / "times"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "NOVA_CI_TIMES"))
    }
    env["PYTHONPATH"] = str(API_ROOT)
    if record:
        env["NOVA_CI_TIMES_DIR"] = str(out)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests._ci_times",
            "-p",
            "no:cacheprovider",
            "-q",
            "checks",
            *args,
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    rows = []
    for path in sorted(out.glob("*.jsonl")) if out.exists() else []:
        rows += [json.loads(line) for line in path.read_text().splitlines()]
    return result, rows, out


@pytest.fixture(scope="module")
def plain(tmp_path_factory):
    return run_sample(tmp_path_factory.mktemp("plain"))


@pytest.fixture(scope="module")
def parallel(tmp_path_factory):
    return run_sample(tmp_path_factory.mktemp("parallel"), "-n", "2")


def only_tests(rows):
    return [row for row in rows if row["kind"] == "test"]


@pytest.mark.parametrize("mode", ["plain", "parallel"])
def test_every_test_is_written_exactly_once_with_its_outcome(mode, request):
    _, rows, _ = request.getfixturevalue(mode)
    recorded = only_tests(rows)
    assert sorted(row["nodeid"] for row in recorded) == sorted(NODES)
    assert {row["nodeid"]: row["outcome"] for row in recorded} == NODES


def test_a_setup_error_keeps_its_row_and_has_no_call_time(plain):
    row = next(r for r in only_tests(plain[1]) if r["nodeid"].endswith("test_setup_error"))
    assert row["outcome"] == "error"
    assert row["call"] == 0


def test_a_skip_keeps_its_reason_and_an_xfail_is_not_a_skip(plain):
    by_node = {row["nodeid"].rsplit("::", 1)[-1]: row for row in only_tests(plain[1])}
    assert "no ffmpeg here" in by_node["test_skip"]["reason"]
    assert by_node["test_known_gap"]["outcome"] == "xfailed"


def test_cpu_spent_in_a_child_process_is_counted(plain):
    by_node = {row["nodeid"].rsplit("::", 1)[-1]: row for row in only_tests(plain[1])}
    assert by_node["test_child_cpu"]["cpu"] > 0.05
    # Sleeping is wall time, not CPU.
    assert by_node["test_pass"]["call"] >= 0.05
    assert by_node["test_pass"]["cpu"] < by_node["test_pass"]["call"]


def test_a_parametrised_id_with_colons_stays_in_its_file(plain):
    row = next(r for r in only_tests(plain[1]) if r["nodeid"].endswith("test_param[a::b]"))
    assert row["file"] == "checks/test_sample.py"


@pytest.mark.parametrize("mode,processes", [("plain", 1), ("parallel", 2)])
def test_each_process_records_its_time_before_the_first_test(mode, processes, request):
    _, rows, _ = request.getfixturevalue(mode)
    startups = [row for row in rows if row["kind"] == "startup"]
    assert len(startups) == processes
    assert len({row["worker"] for row in startups}) == processes
    for row in startups:
        assert row["items"] == len(NODES)
        assert row["seconds"] > 0


def test_recording_does_not_change_the_exit_code_and_is_off_by_default(tmp_path, plain):
    unrecorded, rows, out = run_sample(tmp_path, record=False)
    assert rows == [] and not out.exists()
    assert unrecorded.returncode == plain[0].returncode == 1, plain[0].stdout
