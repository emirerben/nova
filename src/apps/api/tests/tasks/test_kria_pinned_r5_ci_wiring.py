"""R5 defect 10: what CI is told to run, and what it does when its database is missing.

Failure-mode list (written before the code; each row is a check below).

  X1  A journey manifest names a XCUITest the workflow never runs. The suggestion-card UI tests
      were declared on the ``kri-756-suggestion-chips`` journey and verified by nothing: the
      screens-leg step ran only the chips test. Every ``KriaUITests/`` entry of that journey must
      be an ``-only-testing`` target AND a ``verify-tests --xctest`` of the step the journey gates.
  X2  A suite that guards the reaper's jsonpath "passes" by skipping. Where the run provides the
      database (``NOVA_REQUIRE_POSTGRES_TESTS=1``, the api shards), a missing or unreachable
      ``*_test`` Postgres must fail the module, in both of its skip sites; a laptop run without
      the variable still skips (the control).
  X3  Test order decides a result. The end-to-end harness replaces ``enqueue_suggester`` with a
      recorder; the task module that binds the name on import, imported for the first time under
      it, kept the recorder, and two freshness tests then failed in the same process. Running the
      polluter first must not change the freshness result.

X2 and X3 run pytest in a child process so the verdict is the real run's exit status.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

API = Path(__file__).resolve().parents[2]
REPO = API.parents[2]
JOURNEY = "kri-756-suggestion-chips"


def _journey_xctests() -> list[str]:
    manifest = json.loads((REPO / "scripts/ci/journey-manifest.json").read_text("utf-8"))
    row = next(r for r in manifest["journeys"] if r["id"] == JOURNEY)
    return [t for t in row["xctests"] if t.startswith("KriaUITests/")]


def _step_gated_on_the_journey() -> str:
    workflow = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text("utf-8"))
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if JOURNEY in str(step.get("if", "")) and "'screens'" in str(step.get("if", ""))
    ]
    assert len(steps) == 1, f"expected one screens-leg step for {JOURNEY}, found {len(steps)}"
    return str(steps[0]["run"])


def test_x1_the_screens_leg_runs_every_ui_test_the_journey_names():
    run = _step_gated_on_the_journey()
    named = _journey_xctests()
    assert named, "the journey names no UI test"
    for test in named:
        assert f"-only-testing:{test}" in run, f"the screens-leg step never runs {test}"
        assert f"--xctest {test.removeprefix('KriaUITests/')}" in run, (
            f"the screens-leg step never verifies {test}"
        )


def _pytest(target: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    full = {k: v for k, v in os.environ.items() if k not in {"CI", "NOVA_REQUIRE_POSTGRES_TESTS"}}
    full.update(env)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider", *args],
        cwd=API,
        env=full,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


UNREACHABLE = "postgresql://nobody@127.0.0.1:1/nova_unreachable_test"
NOT_A_TEST_DB = "postgresql://nobody@127.0.0.1:1/nova_prod"


@pytest.mark.parametrize(
    ("database", "required", "failed"),
    [
        (NOT_A_TEST_DB, True, True),
        (UNREACHABLE, True, True),
        (NOT_A_TEST_DB, False, False),
        (UNREACHABLE, False, False),
    ],
    ids=["no-test-db/required", "unreachable/required", "no-test-db/laptop", "unreachable/laptop"],
)
def test_x2_the_reaper_jsonpath_module_fails_when_the_database_is_required(
    database, required, failed
):
    env = {"DATABASE_URL": database, "CI": "1"}
    if required:
        env["NOVA_REQUIRE_POSTGRES_TESTS"] = "1"

    run = _pytest("tests/tasks/test_reaper_jsonpath.py", env=env)

    tail = (run.stdout + run.stderr)[-1500:]
    if failed:
        assert run.returncode != 0, f"the module passed with no database:\n{tail}"
        assert "NOVA_REQUIRE_POSTGRES_TESTS" in run.stdout + run.stderr, tail
    else:
        # 5 = pytest's "nothing ran": the whole module skipped at collection.
        assert run.returncode in {0, 5}, tail
        assert "skipped" in run.stdout, tail


def test_x3_the_freshness_result_does_not_depend_on_the_harness_running_first():
    if not os.environ.get("DATABASE_URL", "").endswith("_test"):
        pytest.skip("the end-to-end harness needs a *_test database")

    run = _pytest(
        "tests/e2e_suggestions/test_first_prompt.py",
        "tests/evals/test_suggestion_freshness.py",
        env={"DATABASE_URL": os.environ["DATABASE_URL"]},
    )

    assert run.returncode == 0, (run.stdout + run.stderr)[-2500:]
